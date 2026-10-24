import asyncio
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from starlette.concurrency import run_in_threadpool

from conftest import post, sign_in
from phase2_helpers import create_person, detail, open_date, save, settings_payload, setup
from scheduler.app import CSRF
from scheduler.generation import SolverProcess
from scheduler.solver import solve
from solver_helpers import independent_check


def initial(world, count=8):
    sign_in(world)
    assert post(world.client, "/staff/settings", **settings_payload()).status_code == 200
    day = open_date(world.client)
    people = [create_person(world.client, f"Synthetic solver {i}", restricted_qualified=i < 4) for i in range(count)]
    for p in people:
        response = post(world.client, f"/staff/dates/{day['id']}/availability/{p['id']}", expected_revision=0,
                        service_1_available=True, service_2_available=True, confirmation_reason="Synthetic volunteer confirmed")
        assert response.status_code == 200
    return day, people


def review(world, day):
    return detail(world.client, f"/staff/dates/{day['id']}/draft")


def generate(world, day, expected=None):
    return post(world.client, f"/staff/dates/{day['id']}/draft/generate", **(expected or review(world, day)["expected"]))


def database_state(world):
    with world.db.engine.connect() as c:
        return {table: [tuple(r) for r in c.exec_driver_sql(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("schedule_versions", "slots", "rotation_approvals", "vacancy_approvals")}


class Immediate:
    async def run(self, snapshot):
        return await run_in_threadpool(solve, snapshot)


class Controlled:
    def __init__(self, result=None):
        self.entered, self.release = threading.Event(), threading.Event()
        self.result = result
    async def run(self, snapshot):
        result = self.result or await run_in_threadpool(solve, snapshot)
        self.entered.set()
        assert await run_in_threadpool(self.release.wait, 8)
        return result


def test_real_worker_create_regenerate_snapshots_and_revisions(world):
    day, people = initial(world)
    assert generate(world, day).status_code == 200
    first = review(world, day)
    assert len(first["slots"]) == 16 and not first["problems"]
    assert first["metadata"]["status"] == "OPTIMAL"
    assert first["metadata"]["parameters"]["num_search_workers"] == 1
    assert first["metadata"]["tie_break_algorithm"] == "church-scheduler-date-tiebreak-v1"
    assert "worker_elapsed_seconds" in first["metadata"]
    independent_check(first["metadata"]["input_snapshot"], first["slots"])
    assert generate(world, day).status_code == 200
    second = review(world, day)
    assert second["draft"]["id"] == first["draft"]["id"]
    assert second["draft"]["draft_revision"] == 2
    assert first["slots"] == second["slots"]
    assert second["metadata"]["objectives"]["U"] == 16
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM schedule_versions WHERE date_id=?", (day["id"],)).scalar() == 1
        assert len(list(c.exec_driver_sql("SELECT action FROM audit_events WHERE action LIKE 'draft.%'"))) == 2
        assert not list(c.exec_driver_sql("PRAGMA foreign_key_check"))


def test_empty_draft_conservative_diagnostics_and_stale_form(world):
    day, _ = initial(world, 0)
    expected = review(world, day)["expected"]
    assert generate(world, day, expected).status_code == 200
    result = review(world, day)
    assert all(s["volunteer_id"] is None for s in result["slots"])
    assert len(result["diagnostics"]["vacancies"]) == 16
    assert result["metadata"]["objectives"] == {"E": 0, "O": 0, "B": 0, "U": 0}
    assert "different placement" in result["diagnostics"]["explanation"]
    before = database_state(world)
    assert generate(world, day, expected).status_code == 409
    assert database_state(world) == before


def test_locks_preserved_and_invalid_lock_remains_visible(world):
    day, people = initial(world)
    assert generate(world, day).status_code == 200
    result = review(world, day)
    version = result["draft"]["id"]
    with world.db.transaction() as c:
        c.exec_driver_sql("UPDATE slots SET locked=1 WHERE version_id=? AND service_number=1 AND position_number=1", (version,))
        c.exec_driver_sql("UPDATE slots SET locked=1,volunteer_id=NULL,slot_revision=slot_revision+1 WHERE version_id=? AND service_number=1 AND position_number=2", (version,))
        c.exec_driver_sql("UPDATE schedule_versions SET draft_revision=draft_revision+1 WHERE id=?", (version,))
    assert generate(world, day).status_code == 200
    locked = [s for s in review(world, day)["slots"] if s["locked"]]
    assert locked[0]["volunteer_id"] == result["slots"][0]["volunteer_id"]
    assert locked[1]["volunteer_id"] is None
    with world.db.transaction() as c:
        c.exec_driver_sql("UPDATE volunteers SET active=0 WHERE id=?", (locked[0]["volunteer_id"],))
        c.exec_driver_sql("UPDATE settings SET roster_epoch=roster_epoch+1")
    before = database_state(world)
    failed = generate(world, day)
    assert failed.status_code == 409 and "LOCK_CONFLICT" in failed.text
    assert "inactive" in failed.text
    assert database_state(world) == before
    assert any("inactive" in p for p in review(world, day)["problems"])


def approved_pair(world, day, person, version):
    with world.db.transaction() as c:
        c.exec_driver_sql("UPDATE slots SET volunteer_id=NULL WHERE version_id=?", (version,))
        c.exec_driver_sql("UPDATE slots SET volunteer_id=? WHERE version_id=? AND service_number=1 AND position_number=1", (person, version))
        c.exec_driver_sql("UPDATE slots SET volunteer_id=? WHERE version_id=? AND service_number=2 AND position_number=2", (person, version))
        c.exec_driver_sql("INSERT INTO rotation_approvals(id,version_id,date_id,volunteer_id,first_position,second_position,first_revision,second_revision,first_fingerprint,second_fingerprint,reason,approving_user,approved_at) VALUES('test-pair',?,?,?,1,2,1,1,'fixture','fixture','SQL fixture for Phase 3 strict solver test',?,?)",
                          (version, day["id"], person, world.admin_id, world.now[0]))


def test_approved_locked_same_zone_pair_conflicts_unlocked_pair_replaced(world):
    day, people = initial(world)
    assert generate(world, day).status_code == 200
    version = review(world, day)["draft"]["id"]
    approved_pair(world, day, people[0]["id"], version)
    with world.db.transaction() as c:
        c.exec_driver_sql("UPDATE slots SET locked=1 WHERE version_id=? AND volunteer_id IS NOT NULL", (version,))
    before = database_state(world)
    response = generate(world, day)
    assert response.status_code == 409 and "same-zone" in response.text
    assert database_state(world) == before
    with world.db.transaction() as c:
        c.exec_driver_sql("UPDATE slots SET locked=0 WHERE version_id=?", (version,))
    assert generate(world, day).status_code == 200
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT invalidated_at FROM rotation_approvals WHERE id='test-pair'").scalar() is not None
    assert not review(world, day)["problems"]


@pytest.mark.parametrize("change", ["availability", "qualification", "deactivation"])
def test_eligibility_changes_permanently_invalidate_pair_approval(world, change):
    day, people = initial(world)
    assert generate(world, day).status_code == 200
    version = review(world, day)["draft"]["id"]
    approved_pair(world, day, people[0]["id"], version)
    before = database_state(world)["slots"]
    if change == "availability":
        path = f"/staff/dates/{day['id']}/availability/{people[0]['id']}"
        assert post(world.client, path, expected_revision=1, service_1_available=False, service_2_available=True, confirmation_reason="Confirmed change").status_code == 200
    else:
        p = people[0]
        assert post(world.client, f"/staff/volunteers/{p['id']}", expected_revision=p["revision"], display_name=p["display_name"], public_label=p["public_label"],
                    active=change != "deactivation", restricted_qualified=change != "qualification").status_code == 200
    assert database_state(world)["slots"] == before
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT invalidated_at FROM rotation_approvals WHERE id='test-pair'").scalar() is not None


@pytest.mark.parametrize("change", ["availability", "roster", "config", "draft", "slot", "draft_identity", "approval"])
def test_stale_result_rejected_and_single_generation_admission(world, change):
    day, people = initial(world, 0 if change == "approval" else 8)
    assert generate(world, day).status_code == 200
    old = review(world, day)
    controlled = Controlled()
    world.app.state.generation.runner = controlled
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(generate, world, day, old["expected"])
        assert controlled.entered.wait(4)
        busy = generate(world, day, old["expected"])
        assert busy.status_code == 429 and busy.headers["Retry-After"] == "1"
        assert world.client.get("/health/live").status_code == 200
        if change == "availability":
            assert post(world.client, f"/staff/dates/{day['id']}/availability/{people[0]['id']}", expected_revision=1,
                        service_1_available=False, service_2_available=True, confirmation_reason="Confirmed while solving").status_code == 200
        elif change == "roster":
            create_person(world.client, "Concurrent new volunteer")
        elif change == "config":
            assert post(world.client, "/staff/settings", **settings_payload(2, service_1_label="Changed label")).status_code == 200
        else:
            with world.db.transaction() as c:
                if change == "draft": c.exec_driver_sql("UPDATE schedule_versions SET draft_revision=draft_revision+1 WHERE id=?", (old["draft"]["id"],))
                if change == "slot": c.exec_driver_sql("UPDATE slots SET locked=1,slot_revision=slot_revision+1 WHERE version_id=? AND service_number=1 AND position_number=1", (old["draft"]["id"],))
                if change == "approval":
                    c.exec_driver_sql("INSERT INTO vacancy_approvals(id,version_id,date_id,service_number,position_number,slot_revision,fingerprint,reason,approving_user,approved_at) VALUES('race-approval',?,?,1,1,1,'fixture','Concurrent approval fixture',?,?)",
                                      (old["draft"]["id"], day["id"], world.admin_id, world.now[0]))
                    c.exec_driver_sql("UPDATE schedule_versions SET draft_revision=draft_revision+1 WHERE id=?", (old["draft"]["id"],))
                if change == "draft_identity":
                    c.exec_driver_sql("DELETE FROM slots WHERE version_id=?", (old["draft"]["id"],))
                    c.exec_driver_sql("DELETE FROM schedule_versions WHERE id=?", (old["draft"]["id"],))
        newer = database_state(world)
        controlled.release.set()
        response = future.result(timeout=5)
    assert response.status_code == 409, response.text
    assert database_state(world) == newer


def test_volunteer_response_continues_while_generation_running(world):
    person, pin, day, link, volunteer = setup(world)
    controlled = Controlled()
    world.app.state.generation.runner = controlled
    expected = review(world, day)["expected"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(generate, world, day, expected)
        assert controlled.entered.wait(4)
        assert save(volunteer, link, first=True, second=True).status_code == 200
        assert world.client.get("/health/ready").status_code == 200
        controlled.release.set()
        assert pending.result(timeout=5).status_code == 409
    assert review(world, day)["draft"] is None


def test_timezone_change_does_not_refresh_or_stale_existing_date(world):
    day, _ = initial(world, 0)
    assert post(world.client, "/staff/settings", **settings_payload(2, timezone="America/New_York")).status_code == 200
    assert generate(world, day).status_code == 200
    assert review(world, day)["config"]["timezone"] == "America/Los_Angeles"
    controlled = Controlled()
    world.app.state.generation.runner = controlled
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(generate, world, day)
        assert controlled.entered.wait(4)
        assert post(world.client, "/staff/settings", **settings_payload(3, timezone="Europe/London")).status_code == 200
        controlled.release.set()
        assert pending.result(timeout=5).status_code == 200
    assert review(world, day)["config"]["timezone"] == day["timezone_snapshot"]


def test_configuration_stale_draft_is_preserved_without_refresh(world):
    day, _ = initial(world, 0)
    assert generate(world, day).status_code == 200
    before = database_state(world)
    assert post(world.client, "/staff/settings", **settings_payload(2, essential_8=False)).status_code == 200
    assert review(world, day)["stale_configuration"]
    assert generate(world, day).status_code == 409
    assert database_state(world) == before


@pytest.mark.parametrize("status", ["UNKNOWN/TIMEOUT", "MODEL_INVALID", "INFEASIBLE", "WORKER_FAILURE"])
def test_failed_result_preserves_all_draft_state(world, status):
    day, _ = initial(world, 0)
    assert generate(world, day).status_code == 200
    before = database_state(world)
    class Failed:
        async def run(self, snapshot):
            return {"assignments": None, "metadata": {"status": status}, "problems": []}
    world.app.state.generation.runner = Failed()
    assert generate(world, day).status_code == 503
    assert database_state(world) == before


def test_feasible_result_commits_honest_metadata(world):
    from test_solver import status_factory
    from ortools.sat.python import cp_model
    day, _ = initial(world, 2)
    class Limited:
        async def run(self, snapshot):
            return solve(snapshot, solver_factory=status_factory([cp_model.FEASIBLE]))
    world.app.state.generation.runner = Limited()
    assert generate(world, day).status_code == 200
    result = review(world, day)
    assert result["metadata"]["status"] == "FEASIBLE"
    assert result["metadata"]["completed_stages"] == []
    html = world.client.get(f"/staff/dates/{day['id']}/draft").text
    assert "Coverage not proven maximal" in html and "Timeout incumbents may differ" in html


@pytest.mark.parametrize("fault", ["audit", "disk_full", "invalid_output"])
def test_atomic_rollback_and_independent_validation(world, monkeypatch, fault):
    day, _ = initial(world, 2)
    assert generate(world, day).status_code == 200
    before = database_state(world)
    world.app.state.generation.runner = Immediate()
    if fault == "audit":
        def failed(*args, **kwargs): raise RuntimeError("Synthetic audit fault")
        monkeypatch.setattr("scheduler.generation.audit", failed)
    elif fault == "disk_full":
        def full(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE schedule_versions SET draft_revision="):
                raise sqlite3.OperationalError("database or disk is full")
        event.listen(world.db.engine, "before_cursor_execute", full)
    else:
        class Invalid:
            async def run(self, snapshot):
                result = solve(snapshot)
                result["assignments"][0]["volunteer_id"] = "nonexistent"
                return result
        world.app.state.generation.runner = Invalid()
    response = generate(world, day)
    assert response.status_code in (500, 503)
    if fault == "disk_full": event.remove(world.db.engine, "before_cursor_execute", full)
    assert database_state(world) == before


@pytest.mark.parametrize("role", ["admin", "scheduler", "anonymous", "pending", "volunteer"])
def test_generation_and_review_role_authorization(world, role):
    day, _ = initial(world, 0)
    expected = review(world, day)["expected"]
    if role == "scheduler":
        world.client.cookies.clear()
        sign_in(world, "synthetic-scheduler")
    elif role in ("anonymous", "pending", "volunteer"):
        world.client.cookies.clear()
        if role == "pending":
            from conftest import password_login
            assert password_login(world.client).status_code == 200
        if role == "volunteer":
            world.client.cookies.set("__Host-volunteer", "scoped-volunteer-is-not-staff")
    read = world.client.get(f"/staff/dates/{day['id']}/draft", headers={"Accept": "application/json"})
    write = post(world.client, f"/staff/dates/{day['id']}/draft/generate", **expected,
                 **({"csrf": "forged-but-nonempty"} if role in ("anonymous", "volunteer") else {}))
    if role in ("admin", "scheduler"):
        assert read.status_code == write.status_code == 200
    else:
        assert read.status_code in (401, 403) and write.status_code in (401, 403)
        assert database_state(world)["schedule_versions"] == []


def test_origin_csrf_and_no_mutating_get(world):
    day, _ = initial(world, 0)
    path = f"/staff/dates/{day['id']}/draft/generate"
    data = {"csrf": world.client.cookies.get(CSRF), **review(world, day)["expected"]}
    assert world.client.post(path, json=data, headers={"Origin": "https://hostile.test"}).status_code == 403
    assert world.client.post(path, json={**data, "csrf": "forged"}, headers={"Origin": "https://testserver"}).status_code == 403
    assert world.client.get(path).status_code == 405
    assert database_state(world)["schedule_versions"] == []


def test_real_process_crash_and_watchdog_cleanup(monkeypatch):
    import scheduler.generation as generation
    real = asyncio.create_subprocess_exec
    processes = []
    async def launch(*args, **kwargs):
        process = await real(sys.executable, "-c", "import sys; sys.exit(7)", **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(generation.asyncio, "create_subprocess_exec", launch)
    result = asyncio.run(SolverProcess(0.01).run({}))
    assert result["metadata"]["status"] == "WORKER_FAILURE"
    assert all(p.returncode is not None for p in processes)
    async def hang(*args, **kwargs):
        process = await real(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(generation.asyncio, "create_subprocess_exec", hang)
    result = asyncio.run(SolverProcess(0.01).run({}))
    assert result["metadata"]["status"] == "UNKNOWN/TIMEOUT"
    assert all(p.returncode is not None for p in processes)


def test_worker_cancellation_reaps_process(monkeypatch):
    import scheduler.generation as generation
    real = asyncio.create_subprocess_exec
    processes = []
    async def hang(*args, **kwargs):
        process = await real(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(generation.asyncio, "create_subprocess_exec", hang)
    async def exercise():
        task = asyncio.create_task(SolverProcess().run({}))
        while not processes: await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert processes[0].returncode is not None
    asyncio.run(exercise())


def test_busy_commit_preserves_original_draft(world):
    day, _ = initial(world, 0)
    assert generate(world, day).status_code == 200
    before = database_state(world)
    controlled = Controlled()
    world.app.state.generation.runner = controlled
    def short_busy(dbapi, connection_record, connection_proxy):
        dbapi.execute("PRAGMA busy_timeout=50")
    event.listen(world.db.engine, "checkout", short_busy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(generate, world, day)
        assert controlled.entered.wait(4)
        with world.db.transaction():
            controlled.release.set()
            response = pending.result(timeout=4)
    event.remove(world.db.engine, "checkout", short_busy)
    assert response.status_code == 503
    assert "Database temporarily unavailable" in response.text
    assert database_state(world) == before


def test_real_volunteer_session_cannot_access_drafts_or_generate(world):
    person, pin, day, link, volunteer = setup(world)
    assert generate(world, day).status_code == 200
    expected = review(world, day)["expected"]
    path = f"/staff/dates/{day['id']}/draft"
    assert volunteer.get(path).status_code == 401
    assert volunteer.post(path + "/generate", json={"csrf": "nonempty", **expected}, headers={"Origin": "https://testserver"}).status_code == 401
    public = detail(volunteer, f"/availability/{link}")
    own = detail(volunteer, f"/availability/{link}/mine")
    assert not any(word in str(public) + str(own) for word in ("solver_metadata", "slots", "draft_revision", "restricted_qualified"))


def test_availability_repair_replaces_only_unlocked_invalid_assignments(world):
    day, people = initial(world)
    assert generate(world, day).status_code == 200
    version = review(world, day)["draft"]["id"]
    assert post(world.client, f"/staff/dates/{day['id']}/availability/{people[0]['id']}", expected_revision=1,
                service_1_available=False, service_2_available=False, confirmation_reason="Confirmed neither").status_code == 200
    before = review(world, day)
    assert before["problems"]
    assert generate(world, day).status_code == 200
    after = review(world, day)
    assert not after["problems"]
    assert not any(s["volunteer_id"] == people[0]["id"] for s in after["slots"])
    for old, new in zip(before["slots"], after["slots"]):
        assert new["slot_revision"] == old["slot_revision"] + int(new["volunteer_id"] != old["volunteer_id"])


def test_revoked_staff_during_solve_cannot_commit(world):
    day, _ = initial(world, 0)
    controlled = Controlled()
    world.app.state.generation.runner = controlled
    expected = review(world, day)["expected"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(generate, world, day, expected)
        assert controlled.entered.wait(4)
        with world.db.transaction() as c:
            c.exec_driver_sql("UPDATE staff_sessions SET revoked_at=?", (world.now[0],))
        controlled.release.set()
        assert pending.result(timeout=5).status_code == 401
    assert not database_state(world)["schedule_versions"]


@pytest.mark.parametrize("kind", ["crash", "malformed", "timeout"])
def test_actual_failed_subprocess_leaves_no_draft_and_releases_gate(world, monkeypatch, kind):
    import scheduler.generation as generation
    day, _ = initial(world, 0)
    real = asyncio.create_subprocess_exec
    processes = []
    code = {"crash": "raise SystemExit(7)", "malformed": "print('malformed')", "timeout": "import time; time.sleep(60)"}[kind]
    async def launch(*args, **kwargs):
        p = await real(sys.executable, "-c", code, **kwargs)
        processes.append(p)
        return p
    monkeypatch.setattr(generation.asyncio, "create_subprocess_exec", launch)
    world.app.state.generation.runner = SolverProcess(0.01)
    before = database_state(world)
    result = generate(world, day)
    assert result.status_code == 503
    assert database_state(world) == before
    assert processes[0].returncode is not None
    with world.db.engine.connect() as c:
        details = c.exec_driver_sql("SELECT details FROM audit_events WHERE action='draft.generation_failed'").scalar()
        assert details and "input_snapshot" in details
    monkeypatch.setattr(generation.asyncio, "create_subprocess_exec", real)
    world.app.state.generation.runner = SolverProcess()
    assert generate(world, day).status_code == 200


def test_real_worker_does_not_block_volunteer_requests(world, monkeypatch):
    import scheduler.generation as generation
    person, pin, day, link, volunteer = setup(world)
    real = asyncio.create_subprocess_exec
    entered = threading.Event()
    processes = []
    async def delayed(*args, **kwargs):
        p = await real(sys.executable, "-c", "import time,runpy; time.sleep(1); runpy.run_module('scheduler.solver_worker',run_name='__main__')", **kwargs)
        processes.append(p)
        entered.set()
        return p
    monkeypatch.setattr(generation.asyncio, "create_subprocess_exec", delayed)
    expected = review(world, day)["expected"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(generate, world, day, expected)
        assert entered.wait(3)
        assert processes[0].returncode is None
        assert save(volunteer, link, first=True, second=True).status_code == 200
        assert not pending.done()
        assert world.client.get("/health/live").status_code == 200
        assert pending.result(timeout=5).status_code == 409
    assert processes[0].returncode == 0
    assert not database_state(world)["schedule_versions"]
