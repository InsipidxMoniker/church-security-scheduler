"""Removal through real routes, transactions, sessions and scheduling services."""
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from conftest import PASSWORD, password_login, post, sign_in
from phase2_helpers import create_person, detail, save, setup, verify
from scheduler.app import CSRF
from scheduler.db import row
from scheduler.security import Problem, digest
from test_generation import database_state, generate, review
from test_publication import amend, approve, change, edit, historical, prepare, publish


def remove_staff(world, target=None, epoch=1, **extra):
    return post(world.client, f"/staff/users/{target or world.scheduler_id}/delete",
                expected_epoch=epoch, confirmed=True, **extra)


def remove_person(world, person, **extra):
    return post(world.client, f"/staff/volunteers/{person['id']}/delete",
                expected_revision=person["revision"], confirmed=True, **extra)


@pytest.mark.parametrize("role", ["scheduler", "admin"])
def test_staff_removal_revokes_all_sessions_and_restores_identity(world, role):
    sign_in(world)
    with world.db.transaction() as c:
        c.exec_driver_sql("UPDATE admin_users SET role=? WHERE id=?", (role, world.scheduler_id))
    victim = TestClient(world.app, base_url="https://testserver")
    sign_in(world, "synthetic-scheduler", victim)
    pending = TestClient(world.app, base_url="https://testserver")
    assert password_login(pending, "synthetic-scheduler").status_code == 200
    before = time.time()
    assert remove_staff(world).status_code == 200
    assert victim.get("/staff").status_code == 401
    assert pending.get("/mfa").status_code == 401
    assert password_login(victim, "synthetic-scheduler").status_code == 401
    assert world.scheduler_id not in str(detail(world.client, "/staff/users"))
    assert world.scheduler_id in str(detail(world.client, "/staff/users?status=inactive"))
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM staff_sessions WHERE user_id=? AND revoked_at IS NULL", (world.scheduler_id,)).scalar() == 0
        event = row(c, "SELECT * FROM audit_events WHERE action='staff.access_changed' AND object_id=?", (world.scheduler_id,))
        assert event["staff_id"] == world.admin_id and event["actor_type"] == "staff"
        assert before <= event["occurred_at"] <= time.time()
        assert json.loads(event["details"])["after"] == {"active": False, "role": role}
    assert post(world.client, f"/staff/users/{world.scheduler_id}/access", expected_epoch=2, role=role, active=True).status_code == 200
    assert victim.get("/staff").status_code == 401  # Old sessions stay revoked after restore.
    assert password_login(victim, "synthetic-scheduler").status_code == 200
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE staff_id=? AND action='staff.login'", (world.scheduler_id,)).scalar() == 1
        assert not list(c.exec_driver_sql("PRAGMA foreign_key_check"))


def test_self_removal_and_last_admin_protected_on_both_paths(world):
    sign_in(world)
    assert remove_staff(world, world.admin_id).status_code == 409
    assert post(world.client, f"/staff/users/{world.admin_id}/access", expected_epoch=1, role="scheduler", active=True).status_code == 409
    assert post(world.client, f"/staff/users/{world.scheduler_id}/access", expected_epoch=1, role="admin", active=True).status_code == 200
    assert remove_staff(world, world.admin_id).status_code == 409  # Also with another active admin.
    assert post(world.client, f"/staff/users/{world.admin_id}/access", expected_epoch=1, role="admin", active=False).status_code == 409
    assert world.client.get("/staff").status_code == 200


def test_scheduler_cannot_remove_staff(world):
    sign_in(world, "synthetic-scheduler")
    assert remove_staff(world, world.admin_id).status_code == 403
    assert world.client.get(f"/staff/users/{world.admin_id}/delete").status_code == 403


def test_access_service_rejects_non_administrator_without_changes(world):
    with world.db.transaction() as c:
        actor = row(c, "SELECT * FROM admin_users WHERE id=?", (world.scheduler_id,))
        with pytest.raises(Problem) as error:
            world.auth.change_access(c, actor, world.admin_id, 1, "admin", False)
        assert error.value.status == 403
        assert c.exec_driver_sql("SELECT active FROM admin_users WHERE id=?", (world.admin_id,)).scalar() == 1
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE action='staff.access_changed'").scalar() == 0


def test_competing_administrators_cannot_remove_each_other_and_lose_last_admin(world):
    sign_in(world)
    assert post(world.client, f"/staff/users/{world.scheduler_id}/access", expected_epoch=1, role="admin", active=True).status_code == 200
    second = TestClient(world.app, base_url="https://testserver")
    sign_in(world, "synthetic-scheduler", second)
    requests = [(world.client, world.scheduler_id, 2), (second, world.admin_id, 1)]
    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(lambda args: post(args[0], f"/staff/users/{args[1]}/delete", expected_epoch=args[2], confirmed=True).status_code, requests))
    assert sorted(statuses) == [200, 401]
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM admin_users WHERE active=1 AND role='admin'").scalar() == 1


@pytest.mark.parametrize("role", ["admin", "scheduler"])
def test_volunteer_removal_filters_counts_revokes_pin_sessions_and_restores(world, role):
    person, pin, day, link, volunteer = setup(world)
    assert save(volunteer, link, first=True, second=True).status_code == 200
    if role == "scheduler":
        world.client.cookies.clear()
        sign_in(world, "synthetic-scheduler")
    with world.db.engine.connect() as c:
        epoch = c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar()
        history = row(c, "SELECT * FROM availability WHERE volunteer_id=?", (person["id"],))
    assert remove_person(world, person).status_code == 200
    assert detail(world.client, "/staff/volunteers")["total"] == 0
    assert detail(world.client, "/staff/volunteers?status=inactive")["volunteers"][0]["id"] == person["id"]
    assert person["id"] not in str(detail(volunteer, f"/availability/{link}")["volunteers"])
    counts = detail(world.client, f"/staff/dates/{day['id']}")["counts"]
    assert counts["active_total"] == counts["first_total"] == counts["second_total"] == 0
    assert volunteer.get(f"/availability/{link}/mine").status_code == 401
    assert save(volunteer, link, revision=1).status_code == 401
    assert verify(volunteer, link, person["id"], pin).status_code == 401
    with world.db.engine.connect() as c:
        assert row(c, "SELECT * FROM availability WHERE volunteer_id=?", (person["id"],)) == history
        assert c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar() == epoch + 1
        assert c.exec_driver_sql("SELECT count(*) FROM volunteer_sessions WHERE volunteer_id=? AND revoked_at IS NULL", (person["id"],)).scalar() == 0
        event = row(c, "SELECT * FROM audit_events WHERE action='roster.updated' AND object_id=?", (person["id"],))
        assert event["staff_id"] == (world.admin_id if role == "admin" else world.scheduler_id)
        assert json.loads(event["details"])["after"]["active"] == 0
        assert pin not in event["details"]
    restore = {key: person[key] for key in ("display_name", "public_label", "outside_available")}
    assert post(world.client, f"/staff/volunteers/{person['id']}", **restore, active=True, expected_revision=person["revision"] + 1).status_code == 200
    assert volunteer.get(f"/availability/{link}/mine").status_code == 401
    assert verify(volunteer, link, person["id"], pin).status_code == 200
    assert detail(volunteer, f"/availability/{link}/mine")["response"]["revision"] == 1


@pytest.mark.parametrize("locked", [False, True])
def test_removed_volunteer_retains_invalid_draft_and_blocks_publication_until_repair(world, locked):
    day, people = prepare(world, 2)
    victim = people[0]
    slots = review(world, day)["slots"]
    assigned = next(s for s in slots if s["volunteer_id"] == victim["id"])
    if locked:
        assert edit(world, day, [change(assigned, locked=True)]).status_code == 200
    before = database_state(world)
    stale_expected = review(world, day)["expected"]
    assert remove_person(world, victim).status_code == 200
    assert database_state(world) == before
    current = review(world, day)
    assert any("inactive" in p for p in current["problems"])
    assert "inactive" in world.client.get(f"/staff/dates/{day['id']}/draft").text
    assert publish(world, day).status_code == 409
    assert generate(world, day, stale_expected).status_code == 409
    if locked:
        result = generate(world, day)
        assert result.status_code == 409 and "LOCK_CONFLICT" in result.text
        assert database_state(world) == before
        # Unlock first, retaining the invalid assignment for incremental repair.
        assert edit(world, day, [change(assigned, locked=False)]).status_code == 200
    assert generate(world, day).status_code == 200
    current = review(world, day)
    assert not current["problems"]
    assert all(s["volunteer_id"] != victim["id"] for s in current["slots"])
    assert approve(world, day).status_code == 200
    assert publish(world, day).status_code == 200


def test_volunteer_removed_before_initial_generation_is_never_assigned(world):
    person, _, day, link, volunteer = setup(world)
    assert save(volunteer, link, first=True, second=True).status_code == 200
    assert remove_person(world, person).status_code == 200
    assert generate(world, day).status_code == 200
    result = review(world, day)
    assert not result["problems"]
    assert all(s["volunteer_id"] is None for s in result["slots"])


def test_published_snapshot_attribution_signal_and_pdf_survive_removal_and_restore(world):
    day, people = prepare(world, 1)
    assert approve(world, day).status_code == 200
    version = publish(world, day).json()["version_id"]
    original = historical(world, day, version)
    path = f"/staff/dates/{day['id']}/publications/{version}"
    signal = world.client.get(path + "/signal.txt").text
    def pdf_text():
        return "".join(page.extract_text() for page in PdfReader(io.BytesIO(world.client.get(path + "/schedule.pdf").content)).pages)
    pdf = pdf_text()
    assert amend(world, day, version).status_code == 200
    original = historical(world, day, version)
    assert remove_person(world, people[0]).status_code == 200
    # Another Administrator can remove the original publisher without changing references.
    assert post(world.client, f"/staff/users/{world.scheduler_id}/access", expected_epoch=1, role="admin", active=True).status_code == 200
    world.client.cookies.clear()
    sign_in(world, "synthetic-scheduler")
    assert remove_staff(world, world.admin_id).status_code == 200
    assert historical(world, day, version) == original
    assert world.client.get(path + "/signal.txt").text == signal
    assert pdf_text() == pdf
    assert people[0]["display_name"] in signal and people[0]["display_name"] in pdf
    assert any("inactive" in p for p in review(world, day)["problems"])
    p = people[0]
    assert post(world.client, f"/staff/volunteers/{p['id']}", display_name="Restored different name", public_label="Restored label", outside_available=True, active=True, expected_revision=p["revision"] + 1).status_code == 200
    assert post(world.client, f"/staff/users/{world.admin_id}/access", expected_epoch=2, role="admin", active=True).status_code == 200
    assert historical(world, day, version) == original
    assert world.client.get(path + "/signal.txt").text == signal and pdf_text() == pdf


@pytest.mark.parametrize("kind", ["staff", "volunteer"])
def test_removal_provenance_auth_confirmation_and_safe_get(world, kind):
    sign_in(world)
    if kind == "staff":
        url = f"/staff/users/{world.scheduler_id}/delete"
        data = {"expected_epoch": 1, "confirmed": True}
        table, target = "admin_users", world.scheduler_id
    else:
        person = create_person(world.client, "<Synthetic & Volunteer>")
        url = f"/staff/volunteers/{person['id']}/delete"
        data = {"expected_revision": 1, "confirmed": True}
        table, target = "volunteers", person["id"]
    confirmation = world.client.get(url)
    assert confirmation.status_code == 200 and 'role="dialog"' in confirmation.text
    assert "Historical records will be retained" in confirmation.text
    if kind == "volunteer":
        assert "&lt;Synthetic &amp; Volunteer&gt;" in confirmation.text
    assert post(world.client, url, **{**data, "confirmed": False}).status_code == 422
    assert post(world.client, url, **{k: v for k, v in data.items() if k != "confirmed"}).status_code == 422
    assert post(world.client, url, **data, csrf="invalid").status_code == 403
    for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "https://testserver", "Sec-Fetch-Site": "cross-site"}):
        assert world.client.post(url, json={**data, "csrf": world.client.cookies[CSRF]}, headers=headers).status_code == 403
    anonymous = TestClient(world.app, base_url="https://testserver")
    assert post(anonymous, url, **data, csrf="invalid").status_code == 401
    assert anonymous.get(url).status_code == 401
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql(f"SELECT active FROM {table} WHERE id=?", (target,)).scalar() == 1


@pytest.mark.parametrize("kind", ["staff", "volunteer"])
def test_concurrent_duplicate_and_stale_removals_preserve_newer_updates(world, kind):
    sign_in(world)
    if kind == "staff":
        target = world.scheduler_id
        url, revision = f"/staff/users/{target}/delete", {"expected_epoch": 1}
        table, action = "admin_users", "staff.access_changed"
    else:
        target = create_person(world.client)["id"]
        url, revision = f"/staff/volunteers/{target}/delete", {"expected_revision": 1}
        table, action = "volunteers", "roster.updated"
    clients = [TestClient(world.app, base_url="https://testserver") for _ in range(2)]
    for client in clients:
        client.cookies.update(world.client.cookies)
    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(lambda client: post(client, url, **revision, confirmed=True).status_code, clients))
    assert sorted(statuses) == [200, 409]
    assert post(world.client, url, **revision, confirmed=True).status_code == 409
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql(f"SELECT active FROM {table} WHERE id=?", (target,)).scalar() == 0
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE object_id=? AND action=?", (target, action)).scalar() == 1
    if kind == "staff":
        assert post(world.client, f"/staff/users/{target}/access", expected_epoch=2, role="admin", active=True).status_code == 200
    else:
        assert post(world.client, f"/staff/volunteers/{target}", expected_revision=2, display_name="Newer name", public_label="Newer label", active=True, outside_available=True).status_code == 200
    assert post(world.client, url, **revision, confirmed=True).status_code == 409
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql(f"SELECT active FROM {table} WHERE id=?", (target,)).scalar() == 1


@pytest.mark.parametrize("kind", ["staff", "volunteer"])
def test_audit_failure_rolls_back_removal_and_revocation(world, monkeypatch, kind):
    sign_in(world)
    if kind == "staff":
        target = world.scheduler_id
        module, table = "scheduler.auth.audit", "admin_users"
        with world.db.transaction() as c:
            user = row(c, "SELECT * FROM admin_users WHERE id=?", (target,))
            raw, _ = world.auth.issue(c, user, "pending")
        operation = lambda: remove_staff(world)
    else:
        world.client.cookies.clear()
        world.now[0] += 30
        person, _, _, _, _ = setup(world)
        target = person["id"]
        module, table = "scheduler.volunteer_flow.audit", "volunteers"
        operation = lambda: remove_person(world, person)
    with world.db.engine.connect() as c:
        epoch = c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar()
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")
    monkeypatch.setattr(module, fail)
    assert operation().status_code == 500
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql(f"SELECT active FROM {table} WHERE id=?", (target,)).scalar() == 1
        if kind == "staff":
            assert row(c, "SELECT revoked_at FROM staff_sessions WHERE token_hash=?", (digest(raw),))["revoked_at"] is None
        else:
            assert c.exec_driver_sql("SELECT count(*) FROM volunteer_sessions WHERE revoked_at IS NULL").scalar() == 1
        assert c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar() == epoch
