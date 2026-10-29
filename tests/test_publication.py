"""Phase 4 acceptance on actual migrated, on-disk SQLite databases."""
import copy
import hashlib
import io
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from conftest import post, sign_in
from phase2_helpers import create_person, detail, open_date, settings_payload, setup, save
from test_generation import initial, generate, review, database_state, Immediate
from scheduler.backup import backup_database, restore_database, verify
from scheduler.db import Database, row


def prepare(world, count=0):
    day, people = initial(world, count)
    world.app.state.generation.runner = Immediate()
    assert generate(world, day).status_code == 200
    return day, people


def approve(world, day, keys=None, reason="Private synthetic confirmation"):
    data = review(world, day)
    keys = keys or [(s["service_number"], s["position_number"]) for s in data["slots"] if s["essential"] and not s["volunteer_id"]]
    return post(world.client, f"/staff/dates/{day['id']}/draft/vacancies", **data["expected"], slot_keys=", ".join(f"{s}:{p}" for s,p in keys), reason=reason)


def publish(world, day, expected=None, key="synthetic-key", confirmed=True):
    return post(world.client, f"/staff/dates/{day['id']}/draft/publish", **(expected or review(world, day)["expected"]), idempotency_key=key, confirmed=confirmed)


def edit(world, day, changes, reasons=None, expected=None):
    return post(world.client, f"/staff/dates/{day['id']}/draft/edit", **(expected or review(world, day)["expected"]), changes=changes, rotation_reasons=reasons or {})


def change(slot, **values):
    return {**{k: slot[k] for k in ("service_number", "position_number", "volunteer_id", "locked")}, **values}


def historical(world, day, version):
    return detail(world.client, f"/staff/dates/{day['id']}/publications/{version}")


def amend(world, day, base):
    return post(world.client, f"/staff/dates/{day['id']}/amend", base_version_id=base)


def test_T17_T18_T31_T35_vacancy_review_publication_and_replay(world):
    day, _ = prepare(world)
    before = database_state(world)
    assert publish(world, day).status_code == 409
    assert database_state(world) == before
    assert approve(world, day, reason="  ").status_code == 422
    assert approve(world, day).status_code == 200
    expected = review(world, day)["expected"]
    assert publish(world, day, expected, confirmed=False).status_code == 422
    result = publish(world, day, expected)
    assert result.status_code == 200, result.text
    version = result.json()["version_id"]
    assert publish(world, day, expected).json() == {"version_id": version, "replayed": True}
    assert publish(world, day, {**expected, "draft_revision": 999}).status_code == 409
    assert publish(world, day, expected, "other-key").status_code == 409
    data = historical(world, day, version)
    assert data["current"] and len(data["payload"]["slots"]) == 16
    assert len(data["payload"]["vacancy_approvals"]) == 16
    with world.db.engine.connect() as c:
        day_row = row(c, "SELECT * FROM service_dates WHERE id=?", (day["id"],))
        stored = row(c, "SELECT * FROM schedule_versions WHERE id=?", (version,))
        assert day_row["availability_open"] == 0 and day_row["first_published_at"]
        assert hashlib.sha256(stored["published_payload"].encode()).hexdigest() == stored["payload_hash"]
        actions = [r[0] for r in c.exec_driver_sql("SELECT action FROM audit_events WHERE staff_id=?", (world.admin_id,))]
        assert actions.count("vacancy.approved") == 16 and actions.count("schedule.published") == 1


def test_T19_optional_vacancies_warn_without_approval(world):
    day, _ = prepare(world)
    assert post(world.client, "/staff/settings", **settings_payload(2, **{f"essential_{p}": False for p in range(1,9)})).status_code == 200
    assert publish(world, day).status_code == 409
    assert post(world.client, f"/staff/dates/{day['id']}/draft/refresh", **review(world, day)["expected"]).status_code == 200
    assert "WARNING: optional vacancy" in world.client.get(f"/staff/dates/{day['id']}/draft").text
    assert publish(world, day).status_code == 200


def test_X03_X19_eligible_intentional_vacancy_nonresurrection_and_noop(world):
    day, people = prepare(world, 8)
    sign = next(s for s in review(world, day)["slots"] if s["volunteer_id"])
    assert edit(world, day, [change(sign, volunteer_id=None)]).status_code == 200
    assert publish(world, day).status_code == 409
    key = (sign["service_number"], sign["position_number"])
    assert approve(world, day, [key]).status_code == 200
    unchanged = review(world, day)
    empty = next(s for s in unchanged["slots"] if (s["service_number"],s["position_number"]) == key)
    assert edit(world, day, [change(empty)]).status_code == 200
    assert review(world, day)["expected"] == unchanged["expected"]
    assert edit(world, day, [change(empty, volunteer_id=sign["volunteer_id"])]).status_code == 200
    filled = next(s for s in review(world, day)["slots"] if (s["service_number"],s["position_number"]) == key)
    assert edit(world, day, [change(filled, volunteer_id=None)]).status_code == 200
    assert publish(world, day).status_code == 409
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT invalidated_at FROM vacancy_approvals").scalar() is not None
    # Same Scheduler may approve and publish as separate audited actions.
    world.client.cookies.clear()
    sign_in(world, "synthetic-scheduler")
    assert approve(world, day, [key]).status_code == 200
    result = publish(world, day)
    assert result.status_code == 200
    data = historical(world, day, result.json()["version_id"])["payload"]
    assert data["publisher"] == world.scheduler_id
    assert any(s["vacancy_approved"] for s in data["slots"])


def test_T23_X18_incremental_repair_unlock_and_unrelated_rejection(world):
    day, people = prepare(world, 8)
    slots = review(world, day)["slots"]
    bad = [next(s for s in slots if s["volunteer_id"] == p["id"] and s["service_number"] == 1) for p in people[:2]]
    assert edit(world, day, [change(bad[0], locked=True)]).status_code == 200
    for p in people[:2]:
        assert post(world.client, f"/staff/dates/{day['id']}/availability/{p['id']}", expected_revision=1, service_1_available=False, service_2_available=True, confirmation_reason="Confirmed withdrawal").status_code == 200
    current = review(world, day)
    assert current["problems"] and publish(world, day).status_code == 409
    unrelated = next(s for s in current["slots"] if s["volunteer_id"] not in [p["id"] for p in people[:2]])
    state = database_state(world)
    assert edit(world, day, [change(unrelated, locked=True)]).status_code == 409
    assert database_state(world) == state
    first = next(s for s in current["slots"] if s["service_number"] == 1 and s["position_number"] == bad[0]["position_number"])
    assert edit(world, day, [change(first, locked=False)]).status_code == 200
    assert edit(world, day, [change(first, volunteer_id=None, locked=False)]).status_code == 200
    assert review(world, day)["problems"]
    assert generate(world, day).status_code == 200
    assert not review(world, day)["problems"]
    assert approve(world, day).status_code == 200
    assert publish(world, day).status_code == 200


def test_T05_T06_T14_X04_exact_rotation_approval_locked_regeneration_and_publication(world):
    day, people = prepare(world, 1)
    # Clear generated assignments, then create a deliberately same-zone pair.
    slots = review(world, day)["slots"]
    changes = [change(s, volunteer_id=people[0]["id"] if (s["service_number"],s["position_number"]) in ((1,1),(2,2)) else None, locked=True) for s in slots]
    before = database_state(world)
    assert edit(world, day, changes).status_code == 409
    assert database_state(world) == before
    assert edit(world, day, changes, {people[0]["id"]:"Confirmed exact pair"}).status_code == 200
    assert generate(world, day).status_code == 409
    assert not review(world, day)["problems"]
    assert approve(world, day).status_code == 200
    first = publish(world, day)
    assert first.status_code == 200
    assert amend(world, day, first.json()["version_id"]).status_code == 200
    assert review(world, day)["problems"]  # New version requires a new exact-pair approval.
    unchanged_pair = next(s for s in review(world, day)["slots"] if s["volunteer_id"])
    assert edit(world, day, [change(unchanged_pair)], {people[0]["id"]:"Fresh amendment pair approval"}).status_code == 200
    assert not review(world, day)["problems"]
    assert approve(world, day).status_code == 200
    assert publish(world, day, key="fresh-pair-amendment").status_code == 200


@pytest.mark.parametrize("fault", ["unconfirmed", "configuration", "slot_count", "inactive", "qualification", "unavailable", "approval_fingerprint", "base"])
def test_negative_publication_validation_is_atomic(world, fault):
    day, people = prepare(world, 1 if fault in ("inactive", "qualification", "unavailable") else 0)
    assert approve(world, day).status_code == 200
    expected = review(world, day)["expected"]
    with world.db.transaction() as c:
        if fault == "unconfirmed": c.exec_driver_sql("UPDATE settings SET essentiality_confirmed=0")
        if fault == "configuration": c.exec_driver_sql("UPDATE settings SET config_epoch=config_epoch+1")
        if fault == "slot_count":
            c.exec_driver_sql("DELETE FROM vacancy_approvals WHERE service_number=1 AND position_number=8")
            c.exec_driver_sql("DELETE FROM slots WHERE service_number=1 AND position_number=8")
        if fault == "inactive": c.exec_driver_sql("UPDATE volunteers SET active=0")
        if fault == "qualification":
            c.exec_driver_sql("UPDATE volunteers SET outside_available=0")
            c.exec_driver_sql("UPDATE slots SET volunteer_id=NULL WHERE service_number=1")
            c.exec_driver_sql("UPDATE slots SET volunteer_id=? WHERE service_number=1 AND position_number=1", (people[0]["id"],))
        if fault == "unavailable": c.exec_driver_sql("UPDATE availability SET service_1_available=0,service_2_available=0")
        if fault == "approval_fingerprint": c.exec_driver_sql("UPDATE vacancy_approvals SET fingerprint='forged'")
        if fault == "base": c.exec_driver_sql("UPDATE schedule_versions SET base_version_id=id")
    before = database_state(world)
    assert publish(world, day, expected).status_code == 409
    assert database_state(world) == before


@pytest.mark.parametrize("fault", ["audit", "disk"])
def test_X06_transaction_fault_rolls_back_publication(world, monkeypatch, fault):
    day, _ = prepare(world)
    assert approve(world, day).status_code == 200
    expected = review(world, day)["expected"]
    before = database_state(world)
    if fault == "audit":
        def failed(*args, **kwargs): raise RuntimeError("Synthetic audit failure")
        monkeypatch.setattr("scheduler.publication.audit", failed)
    else:
        def disk(conn, cursor, statement, params, context, many):
            if statement.startswith("INSERT INTO idempotency"):
                raise sqlite3.OperationalError("database or disk is full")
        event.listen(world.db.engine, "before_cursor_execute", disk)
    try:
        assert publish(world, day, expected).status_code in (500,503)
    finally:
        if fault == "disk": event.remove(world.db.engine, "before_cursor_execute", disk)
    assert database_state(world) == before
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT current_published_version_id FROM service_dates").scalar() is None
        assert c.exec_driver_sql("SELECT count(*) FROM idempotency").scalar() == 0


def test_T25_T32_X12_amendment_fresh_approvals_and_historical_consistency(world):
    person, pin, day, link, volunteer = setup(world)
    assert save(volunteer, link, first=True, second=True).status_code == 200
    world.app.state.generation.runner = Immediate()
    assert generate(world, day).status_code == 200
    assert approve(world, day).status_code == 200
    first = publish(world, day).json()["version_id"]
    original = historical(world, day, first)["payload"]
    path = f"/staff/dates/{day['id']}/publications/{first}"
    text = world.client.get(path + "/signal.txt").text
    pdf_text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(world.client.get(path + "/schedule.pdf").content)).pages)
    assert save(volunteer, link, revision=1).status_code in (401,403,410)
    assert amend(world, day, first).status_code == 200
    assert amend(world, day, first).json()["replayed"]
    assert publish(world, day, key="amend").status_code == 409
    assert historical(world, day, first)["current"]
    p = detail(world.client, f"/staff/volunteers/{person['id']}")["person"]
    assert post(world.client, f"/staff/volunteers/{person['id']}", expected_revision=p["revision"], display_name="Renamed synthetic", public_label="Renamed synthetic", active=False, outside_available=False).status_code == 200
    assert review(world, day)["problems"]
    assert historical(world, day, first)["payload"] == original
    assert world.client.get(path + "/signal.txt").text == text
    assert "".join(p.extract_text() for p in PdfReader(io.BytesIO(world.client.get(path + "/schedule.pdf").content)).pages) == pdf_text
    assert generate(world, day).status_code == 200
    assert approve(world, day).status_code == 200
    second = publish(world, day, key="amend").json()
    assert second["publication_number"] == 2
    old = historical(world, day, first)
    assert old["payload"] == original and not old["current"]
    assert "SUPERSEDED" in old["signal_text"]
    assert "AMENDED" in historical(world, day, second["version_id"])["signal_text"]
    assert save(volunteer, link, revision=1).status_code in (401,403,410)


@pytest.mark.parametrize("same_key", [True, False])
def test_X07_real_concurrent_publish_and_amend_requests(world, same_key):
    day, _ = prepare(world)
    assert approve(world, day).status_code == 200
    expected = review(world, day)["expected"]
    barrier = threading.Barrier(2)
    def attempt(index):
        barrier.wait()
        return publish(world, day, expected, "same" if same_key else str(index))
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, (1,2)))
    assert sorted(r.status_code for r in results) == ([200,200] if same_key else [200,409])
    base = next(r.json()["version_id"] for r in results if r.status_code == 200)
    with ThreadPoolExecutor(2) as pool:
        drafts = list(pool.map(lambda _: amend(world, day, base), (1,2)))
    assert all(r.status_code == 200 for r in drafts)
    assert len({r.json()["draft_id"] for r in drafts}) == 1
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM schedule_versions WHERE status='published'").scalar() == 1
        assert c.exec_driver_sql("SELECT count(*) FROM schedule_versions WHERE status='draft'").scalar() == 1


@pytest.mark.parametrize("input_change", ["availability", "roster", "config"])
def test_X05_committed_input_change_wins_before_waiting_publication(world, input_change):
    day, people = prepare(world, 1)
    assert approve(world, day).status_code == 200
    expected = review(world, day)["expected"]
    started = threading.Event()
    def attempt():
        started.set()
        return publish(world, day, expected)
    with ThreadPoolExecutor(1) as pool:
        with world.db.transaction() as c:
            pending = pool.submit(attempt)
            assert started.wait(2)
            if input_change == "availability":
                c.exec_driver_sql("UPDATE availability SET service_1_available=0,service_2_available=0")
                c.exec_driver_sql("UPDATE service_dates SET date_input_epoch=date_input_epoch+1")
            elif input_change == "roster":
                c.exec_driver_sql("UPDATE volunteers SET outside_available=0")
                c.exec_driver_sql("UPDATE settings SET roster_epoch=roster_epoch+1")
            else:
                c.exec_driver_sql("UPDATE settings SET config_epoch=config_epoch+1")
        assert pending.result(timeout=5).status_code == 409
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM schedule_versions WHERE status='published'").scalar() == 0


def test_X05_publication_commits_before_waiting_volunteer_write(world, monkeypatch):
    person, pin, day, link, volunteer = setup(world)
    world.app.state.generation.runner = Immediate()
    assert generate(world, day).status_code == 200
    assert approve(world, day).status_code == 200
    expected = review(world, day)["expected"]
    entered, release = threading.Event(), threading.Event()
    from scheduler.publication import audit as real_audit
    def paused(c, action, *args, **kwargs):
        if action == "schedule.published":
            entered.set()
            assert release.wait(4)
        return real_audit(c, action, *args, **kwargs)
    monkeypatch.setattr("scheduler.publication.audit", paused)
    with ThreadPoolExecutor(2) as pool:
        publishing = pool.submit(publish, world, day, expected)
        assert entered.wait(3)
        writing = pool.submit(save, volunteer, link)
        release.set()
        assert publishing.result(timeout=5).status_code == 200
        assert writing.result(timeout=5).status_code in (401,403,410)


def test_X09_authorization_csrf_substitution_and_exports(world):
    day, _ = prepare(world)
    assert approve(world, day).status_code == 200
    version = publish(world, day).json()["version_id"]
    path = f"/staff/dates/{day['id']}/publications/{version}"
    anonymous = TestClient(world.app, base_url="https://testserver")
    for suffix in ("", "/signal.txt", "/schedule.pdf"):
        assert anonymous.get(path + suffix).status_code == 401
        response = world.client.get(path + suffix)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    other = open_date(world.client, "2026-11-01")
    assert world.client.get(f"/staff/dates/{other['id']}/publications/{version}").status_code == 404
    for mutation in ("amend", "draft/vacancies", "draft/publish", "draft/edit", "draft/refresh", "draft/slot", "draft/swap"):
        p = f"/staff/dates/{day['id']}/{mutation}"
        assert world.client.post(p, json={}, headers={"Origin":"https://hostile.test"}).status_code == 403
        assert world.client.get(p).status_code == 405
    assert post(world.client, f"/staff/dates/{day['id']}/amend", csrf="forged", base_version_id=version).status_code == 403


def test_X11_X14_safe_unicode_exports_no_private_data_or_resource_fetch(world, monkeypatch):
    day, people = prepare(world, 1)
    p = people[0]
    name = "Élodie <img src='https://invalid.example/secret'> & Ω Ж"
    assert post(world.client, f"/staff/volunteers/{p['id']}", expected_revision=1, display_name=name, public_label=name, active=True, outside_available=True).status_code == 200
    assert approve(world, day, reason="DO-NOT-EXPORT-private-reason").status_code == 200
    version = publish(world, day).json()["version_id"]
    path = f"/staff/dates/{day['id']}/publications/{version}"
    def forbidden(*args, **kwargs): raise AssertionError("Renderer attempted external access")
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    text = world.client.get(path + "/signal.txt").text
    result = world.client.get(path + "/schedule.pdf")
    assert result.status_code == 200 and result.content.startswith(b"%PDF-")
    assert 'attachment; filename="security-2026-10-25-v1.pdf"' == result.headers["Content-Disposition"]
    reader = PdfReader(io.BytesIO(result.content))
    extracted = "\n".join(p.extract_text() for p in reader.pages)
    for exported in (text, extracted):
        assert "Élodie" in exported and "Ω Ж" in exported
        assert "DO-NOT-EXPORT" not in exported and "pin_hash" not in exported and "credential_epoch" not in exported
    assert len(reader.pages) <= 2
    assert "&lt;img" in world.client.get(path).text


def test_T38_T39_backup_clean_restore_and_immutable_outputs(world, tmp_path):
    person, pin, day, link, volunteer = setup(world)
    world.app.state.generation.runner = Immediate()
    assert generate(world, day).status_code == 200
    assert approve(world, day).status_code == 200
    first = publish(world, day).json()["version_id"]
    assert amend(world, day, first).status_code == 200
    assert approve(world, day).status_code == 200
    second = publish(world, day, key="amend").json()["version_id"]
    source_payloads = [historical(world, day, v)["payload"] for v in (first,second)]
    backup, restored = tmp_path / "backup.sqlite", tmp_path / "clean.sqlite"
    backup_database(world.db.path, backup)
    verify(backup)
    from cryptography.fernet import Fernet
    wrong_key = tmp_path / "wrong-recovery-key"
    wrong_key.write_bytes(Fernet.generate_key())
    failed_destination = tmp_path / "must-not-be-published.sqlite"
    with pytest.raises(RuntimeError): restore_database(backup, failed_destination, wrong_key)
    assert not failed_destination.exists()
    with pytest.raises(FileExistsError): backup_database(world.db.path, backup)
    key = tmp_path / "external-key"
    restore_database(backup, restored, key)
    db = Database(restored)
    try:
        db.check()
        with db.engine.connect() as c:
            assert [json.loads(r[0]) for r in c.exec_driver_sql("SELECT published_payload FROM schedule_versions ORDER BY publication_number")] == source_payloads
            assert c.exec_driver_sql("SELECT count(*) FROM staff_sessions WHERE revoked_at IS NULL").scalar() == 0
            assert c.exec_driver_sql("SELECT count(*) FROM volunteer_sessions WHERE revoked_at IS NULL").scalar() == 0
            assert c.exec_driver_sql("SELECT count(*) FROM date_links WHERE revoked_at IS NULL").scalar() == 0
            assert c.exec_driver_sql("SELECT sum(availability_open) FROM service_dates").scalar() == 0
            assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert c.exec_driver_sql("SELECT count(*) FROM volunteer_sessions").scalar() == 1
            assert c.exec_driver_sql("SELECT count(*) FROM date_links").scalar() == 1
        from scheduler.app import create_app
        app = create_app(db, world.security, "https://testserver", lambda: world.now[0])
        world.now[0] += 30
        with TestClient(app, base_url="https://testserver") as recovered:
            sign_in(world, client=recovered)
            assert detail(recovered, f"/staff/dates/{day['id']}/publications/{second}")["current"]
            assert recovered.get(f"/availability/{link}").status_code == 404
        with pytest.raises(RuntimeError): restore_database(backup, restored, key)
    finally:
        db.engine.dispose()


def test_X20_timezone_refresh_and_amendment_preserve_date(world):
    day, _ = prepare(world)
    old = review(world, day)
    assert post(world.client, "/staff/settings", **settings_payload(2, timezone="Europe/London")).status_code == 200
    assert review(world, day)["expected"] == old["expected"]
    assert approve(world, day).status_code == 200
    first = publish(world, day).json()["version_id"]
    assert amend(world, day, first).status_code == 200
    assert review(world, day)["config"]["timezone"] == "America/Los_Angeles"
    assert post(world.client, "/staff/settings", **settings_payload(3, timezone="Europe/London", service_1_label="Changed", essential_8=False)).status_code == 200
    assert review(world, day)["stale_configuration"]
    assert post(world.client, f"/staff/dates/{day['id']}/draft/refresh", **review(world, day)["expected"]).status_code == 200
    assert review(world, day)["config"]["timezone"] == "America/Los_Angeles"
    assert not review(world, day)["stale_configuration"]
    assert open_date(world.client, "2026-11-01")["timezone_snapshot"] == "Europe/London"


def test_X03_exact_pair_A_B_A_never_revives_and_unrelated_edits_survive(world):
    day, people = prepare(world, 1)
    person = people[0]["id"]
    slots = review(world, day)["slots"]
    changes = [change(s, volunteer_id=person if (s["service_number"], s["position_number"]) in ((1,1),(2,2)) else None) for s in slots]
    assert edit(world, day, changes, {person:"Exact A pair"}).status_code == 200
    first = review(world, day)
    approval_id = first["rotation_approvals"][0]["id"]
    noop = next(s for s in first["slots"] if s["volunteer_id"])
    assert edit(world, day, [change(noop)]).status_code == 200
    assert review(world, day)["expected"] == first["expected"]
    unrelated = next(s for s in first["slots"] if s["position_number"] == 8 and s["service_number"] == 1)
    assert edit(world, day, [change(unrelated, locked=True)]).status_code == 200
    assert review(world, day)["rotation_approvals"][0]["id"] == approval_id
    second = next(s for s in first["slots"] if s["volunteer_id"] and s["service_number"] == 2)
    assert edit(world, day, [change(second, volunteer_id=None)]).status_code == 200
    before = database_state(world)
    assert edit(world, day, [change(second)]).status_code == 409
    assert database_state(world) == before
    assert edit(world, day, [change(second)], {person:"Fresh approval after B"}).status_code == 200
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT invalidated_at FROM rotation_approvals WHERE id=?", (approval_id,)).scalar() is not None
    assert review(world, day)["rotation_approvals"][0]["id"] != approval_id


def test_T30_swap_atomic_revision_conflict_and_availability_amendment(world):
    day, people = prepare(world, 8)
    data = review(world, day)
    a, b = [s for s in data["slots"] if s["service_number"] == 1 and s["zone"] == "inside"][:2]
    moves = [change(a, volunteer_id=b["volunteer_id"]), change(b, volunteer_id=a["volunteer_id"])]
    assert edit(world, day, moves, expected=data["expected"]).status_code == 200
    assert edit(world, day, [change(a)], expected=data["expected"]).status_code == 409
    assert publish(world, day).status_code == 200
    base = review(world, day)["day"]["current_published_version_id"]
    p = people[0]
    path = f"/staff/dates/{day['id']}/availability/{p['id']}"
    values = dict(expected_revision=1, service_1_available=False, service_2_available=True, confirmation_reason="Volunteer confirmed")
    assert post(world.client, path, **values).status_code == 409
    assert amend(world, day, base).status_code == 200
    assert post(world.client, path, **values).status_code == 200
    assert post(world.client, path, **values).status_code == 409
    assert review(world, day)["problems"]


def test_busy_publication_returns_retry_without_partial_changes(world):
    day, _ = prepare(world)
    assert approve(world, day).status_code == 200
    expected = review(world, day)["expected"]
    before = database_state(world)
    def timeout(dbapi, record, proxy): dbapi.execute("PRAGMA busy_timeout=40")
    event.listen(world.db.engine, "checkout", timeout)
    try:
        with ThreadPoolExecutor(1) as pool:
            with world.db.transaction():
                result = pool.submit(publish, world, day, expected).result(timeout=3)
        assert result.status_code == 503 and result.headers["Retry-After"] == "5"
    finally:
        event.remove(world.db.engine, "checkout", timeout)
    assert database_state(world) == before


def test_concurrent_staff_edits_exactly_one_commit(world):
    day, _ = prepare(world)
    data = review(world, day)
    barrier = threading.Barrier(2)
    def attempt(index):
        barrier.wait()
        return edit(world, day, [change(data["slots"][index], locked=True)], expected=data["expected"])
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, (0,1)))
    assert sorted(r.status_code for r in results) == [200,409]
    data = review(world, day)
    assert sum(s["locked"] for s in data["slots"]) == 1


def test_real_volunteer_cannot_view_history_or_export_scheduler_can(world):
    person, pin, day, link, volunteer = setup(world)
    world.app.state.generation.runner = Immediate()
    assert generate(world, day).status_code == 200
    assert approve(world, day).status_code == 200
    version = publish(world, day).json()["version_id"]
    path = f"/staff/dates/{day['id']}/publications/{version}"
    for suffix in ("", "/signal.txt", "/schedule.pdf"):
        assert volunteer.get(path + suffix).status_code == 401
    world.client.cookies.clear()
    sign_in(world, "synthetic-scheduler")
    for suffix in ("", "/signal.txt", "/schedule.pdf"):
        assert world.client.get(path + suffix).status_code == 200


def test_pdf_work_is_bounded_and_gate_recovers(world):
    from scheduler.exports import PDF_GATE
    day, _ = prepare(world)
    assert approve(world, day).status_code == 200
    version = publish(world, day).json()["version_id"]
    path = f"/staff/dates/{day['id']}/publications/{version}/schedule.pdf"
    PDF_GATE.acquire()
    PDF_GATE.acquire()
    try:
        result = world.client.get(path)
        assert result.status_code == 429 and result.headers["Retry-After"] == "2"
    finally:
        PDF_GATE.release()
        PDF_GATE.release()
    assert world.client.get(path).status_code == 200


def test_published_pointer_cannot_be_removed_or_rolled_back(world):
    day, _ = prepare(world)
    assert approve(world, day).status_code == 200
    first = publish(world, day).json()["version_id"]
    assert amend(world, day, first).status_code == 200
    assert approve(world, day).status_code == 200
    assert publish(world, day, key="second").status_code == 200
    for value in (None, first):
        with pytest.raises(IntegrityError), world.db.transaction() as c:
            c.exec_driver_sql("UPDATE service_dates SET current_published_version_id=? WHERE id=?", (value, day["id"]))
