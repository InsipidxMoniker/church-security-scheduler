import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from scheduler.app import CSRF
from scheduler.db import row
from scheduler.phase2_routes import VOLUNTEER, VOLUNTEER_CSRF
from scheduler.security import digest, Problem
from conftest import post, sign_in
from phase2_helpers import create_person, detail, new_link, open_date, save, settings_payload, setup, verify


@pytest.mark.parametrize("first,second,choice", [(True,False,"first"),(False,True,"second"),(True,True,"both"),(False,False,"neither")])
def test_atomic_responses_self_visibility_audit_and_counts(world, first, second, choice):
    person, _, day, link, client = setup(world)
    untouched = create_person(world.client, "Synthetic no response")
    own = detail(client, f"/availability/{link}/mine")
    assert own["response"] == {"service_1_available":None,"service_2_available":None,"revision":0,"choice":None}
    saved = save(client, link, first=first, second=second)
    assert saved.status_code == 200 and saved.json()["choice"] == choice and saved.json()["revision"] == 1
    assert save(client, link, revision=0).status_code == 409
    latest = detail(client, f"/availability/{link}/mine")["response"]
    assert latest["choice"] == choice
    assert type(latest["service_1_available"]) is bool and type(latest["service_2_available"]) is bool
    dashboard = detail(world.client, f"/staff/dates/{day['id']}")
    counts = dashboard["counts"]
    assert sum(counts[b] for b in ("first_only","second_only","both","neither","no_response")) == counts["active_total"] == 2
    assert counts["no_response"] == 1 and counts["first_total"] == int(first) and counts["second_total"] == int(second)
    assert dashboard["day"]["date_input_epoch"] == day["date_input_epoch"] + 1
    with world.db.engine.connect() as c:
        assert row(c, "SELECT * FROM availability WHERE date_id=? AND volunteer_id=?", (day["id"], untouched["id"])) is None
        event = row(c, "SELECT * FROM audit_events WHERE action='availability.updated'")
        assert event["actor_type"] == "volunteer" and event["volunteer_id"] == person["id"] and event["staff_id"] is None
        assert json.loads(event["details"])["after"]["choice"] == choice


def test_dashboard_new_inactive_reactivated_qualified_and_staff_confirmation(world):
    person, _, day, link, client = setup(world)
    save(client, link, first=True, second=True)
    other = create_person(world.client, "Synthetic qualified", outside_available=True)
    response = post(world.client, f"/staff/dates/{day['id']}/availability/{other['id']}", expected_revision=0, service_1_available=True, service_2_available=False, confirmation_reason="Confirmed privately by this volunteer")
    assert response.status_code == 200
    count = detail(world.client, f"/staff/dates/{day['id']}")["counts"]
    assert count["first_qualified"] == 1 and count["second_qualified"] == 0 and count["both"] == 1 and count["first_only"] == 1
    post(world.client, f"/staff/volunteers/{person['id']}", expected_revision=person["revision"], display_name=person["display_name"], public_label="", active=False, outside_available=False)
    assert detail(world.client, f"/staff/dates/{day['id']}")["counts"]["active_total"] == 1
    assert client.get(f"/availability/{link}/mine").status_code == 401
    post(world.client, f"/staff/volunteers/{person['id']}", expected_revision=person["revision"]+1, display_name=person["display_name"], public_label="", active=True, outside_available=False)
    assert detail(world.client, f"/staff/dates/{day['id']}")["counts"]["both"] == 1
    assert client.get(f"/availability/{link}/mine").status_code == 401
    create_person(world.client, "Synthetic newly active")
    assert detail(world.client, f"/staff/dates/{day['id']}")["counts"]["no_response"] == 1
    bad = post(world.client, f"/staff/dates/{day['id']}/availability/{other['id']}", expected_revision=1, service_1_available=False, service_2_available=False, confirmation_reason=" ")
    assert bad.status_code == 422
    with world.db.engine.connect() as c:
        response = row(c, "SELECT * FROM availability WHERE volunteer_id=?", (other["id"],))
        assert response["actor_id"] == world.admin_id and response["confirmation_reason"] and response["source"] == "admin"


def test_scheduler_confirmed_response_and_closed_collection_staff_edit(world):
    person, _, day, link, client = setup(world)
    post(world.client, f"/staff/dates/{day['id']}/collection", expected_epoch=day["date_input_epoch"], opened=False)
    other = TestClient(world.app, base_url="https://testserver")
    sign_in(world, "synthetic-scheduler", other)
    recorded = post(other, f"/staff/dates/{day['id']}/availability/{person['id']}", expected_revision=0, service_1_available=False, service_2_available=True, confirmation_reason="Volunteer confirmed second service")
    assert recorded.status_code == 200 and recorded.json()["choice"] == "second"
    assert save(client, link).status_code == 403
    with world.db.engine.connect() as c:
        assert row(c, "SELECT * FROM availability")["actor_id"] == world.scheduler_id


def test_leading_zero_pin_secret_storage_one_time_and_reset_revocation(world, monkeypatch):
    sign_in(world)
    post(world.client, "/staff/settings", **settings_payload())
    person = create_person(world.client)
    sequence = iter("01234567")
    monkeypatch.setattr("scheduler.volunteer_flow.secrets.choice", lambda alphabet: next(sequence))
    result = post(world.client, f"/staff/volunteers/{person['id']}/pin", expected_revision=1)
    assert result.status_code == 200 and result.json()["pin"] == "01234567"
    day = open_date(world.client)
    issued = new_link(world.client, day)
    link = issued["token"]
    client = TestClient(world.app, base_url="https://testserver")
    assert verify(client, link, person["id"], "01234567").status_code == 200
    raw_session = client.cookies[VOLUNTEER]
    assert "01234567" not in detail(world.client, f"/staff/volunteers/{person['id']}").__repr__()
    with world.db.engine.connect() as c:
        stored = row(c, "SELECT * FROM volunteers WHERE id=?", (person["id"],))
        assert stored["pin_hash"].startswith("$argon2id$") and "01234567" not in stored["pin_hash"]
        assert world.security.verify(stored["pin_hash"], "01234567")
        assert not row(c, "SELECT * FROM volunteer_sessions WHERE token_hash=?", (raw_session,))
        assert c.exec_driver_sql("SELECT count(*) FROM volunteer_sessions WHERE token_hash=?", (digest(raw_session),)).scalar() == 1
        before_roster = c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar()
    sequence = iter("09876543")
    reset = post(world.client, f"/staff/volunteers/{person['id']}/pin", expected_revision=2)
    assert reset.json()["pin"] == "09876543"
    assert client.get(f"/availability/{link}/mine").status_code == 401
    assert verify(client, link, person["id"], "01234567").status_code == 401
    assert verify(client, link, person["id"], "09876543").status_code == 200
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar() == before_roster
        assert row(c, "SELECT * FROM volunteers WHERE id=?", (person["id"],))["credential_epoch"] == stored["credential_epoch"]+1
        audit = " ".join(r[0] for r in c.exec_driver_sql("SELECT details FROM audit_events").all())
        assert "01234567" not in audit and "09876543" not in audit and link not in audit and raw_session not in audit
    assert world.client.get(f"/staff/volunteers/{person['id']}/pin").status_code == 405


def test_pin_throttle_across_dates_restart_expiry_and_before_hash(world, monkeypatch):
    person, pin, day, link, client = setup(world)
    second_day = open_date(world.client, "2026-11-01")
    second_link = new_link(world.client, second_day)["token"]
    for index in range(5):
        assert verify(client, link if index%2 else second_link, person["id"], "wrong").status_code == 401
    original = world.security.verify
    monkeypatch.setattr(world.security, "verify", lambda *args: pytest.fail("Blocked verification must not hash"))
    blocked = verify(client, link, person["id"], pin)
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    world.db.engine.dispose()
    assert verify(client, second_link, person["id"], pin).status_code == 429
    monkeypatch.setattr(world.security, "verify", original)
    world.now[0] += 901
    assert verify(client, link, person["id"], pin).status_code == 200


@pytest.mark.parametrize("kind", ["ip","global","roster"])
def test_persistent_ip_global_roster_throttles_and_spoofing(world, kind, monkeypatch):
    person, pin, day, link, client = setup(world)
    flow = world.app.state.flow
    # Small configurable budgets exercise exact same persisted counter path.
    if kind == "ip":
        flow.pin_ip_limit = 2
    elif kind == "global":
        flow.pin_global_limit = 2
    else:
        flow.roster_ip_limit = 2
    with world.db.transaction() as c:
        c.exec_driver_sql("DELETE FROM auth_throttles")
    if kind == "roster":
        for _ in range(2):
            assert client.get(f"/availability/{link}").status_code == 200
        assert client.get(f"/availability/{link}", headers={"X-Real-IP":"192.0.2.2","X-Forwarded-For":"192.0.2.2"}).status_code == 429
        world.now[0] += 61
        assert client.get(f"/availability/{link}").status_code == 200
        return
    for _ in range(2):
        assert verify(client, link, str(uuid.uuid4()), "wrong").status_code == 401
    if kind == "global":
        # Different actual peers still share the global budget.
        client = TestClient(world.app, base_url="https://testserver", client=("192.0.2.44",50000))
    page = detail(client, f"/availability/{link}")
    blocked = client.post(f"/availability/{link}/verify-pin", json={"csrf":page["csrf"],"volunteer_id":person["id"],"pin":pin}, headers={"Origin":"https://testserver","X-Real-IP":"192.0.2.99","X-Forwarded-For":"192.0.2.99"})
    assert blocked.status_code == 429
    world.now[0] += 301
    assert verify(client, link, person["id"], pin).status_code == 200


def test_pin_hash_work_is_bounded(world):
    person, pin, _, link, client = setup(world)
    world.security.hash_slots.acquire()
    world.security.hash_slots.acquire()
    try:
        assert verify(client, link, person["id"], pin).status_code == 429
    finally:
        world.security.hash_slots.release()
        world.security.hash_slots.release()


@pytest.mark.parametrize("state", ["expired_link","rotated","revoked","idle","absolute","epoch","deactivated"])
def test_expiry_rotation_and_session_revocation(world, state):
    person, _, day, link, client = setup(world)
    assert save(client, link).status_code == 200
    if state == "expired_link":
        world.now[0] += 31*86400
    elif state in ("rotated","revoked"):
        current = detail(world.client, f"/staff/dates/{day['id']}")["day"]
        action = "rotate" if state == "rotated" else "revoke"
        result = post(world.client, f"/staff/dates/{day['id']}/link", expected_epoch=current["date_input_epoch"], operation=action)
        assert result.status_code == 200
        assert link not in str(detail(world.client, f"/staff/dates/{day['id']}"))
    elif state == "idle":
        world.now[0] += 1801
    elif state == "absolute":
        with world.db.transaction() as c:
            c.exec_driver_sql("UPDATE volunteer_sessions SET last_used_at=?,expires_at=?", (world.now[0], world.now[0]-1))
    else:
        with world.db.transaction() as c:
            c.exec_driver_sql("UPDATE volunteers SET " + ("active=0" if state == "deactivated" else "credential_epoch=credential_epoch+1") + " WHERE id=?", (person["id"],))
    expected = 404 if state in ("expired_link","rotated","revoked") else 401
    assert client.get(f"/availability/{link}/mine").status_code == expected
    with world.db.engine.connect() as c:
        assert row(c, "SELECT * FROM availability")["revision"] == 1
        if state in ("rotated","revoked"):
            assert row(c, "SELECT * FROM volunteer_sessions")["revoked_at"] is not None


def test_cross_identity_date_staff_access_csrf_and_data_minimization(world):
    person, pin, day, link, client = setup(world)
    other = create_person(world.client, "Synthetic other private response", outside_available=True)
    post(world.client, f"/staff/dates/{day['id']}/availability/{other['id']}", expected_revision=0, service_1_available=False, service_2_available=False, confirmation_reason="PRIVATE_CONFIRMATION_REASON")
    public = detail(client, f"/availability/{link}")
    assert all(set(p) == {"id","public_label"} for p in public["volunteers"])
    assert "outside_available" not in str(public) and "PRIVATE_CONFIRMATION_REASON" not in str(public)
    assert set(detail(client, f"/availability/{link}/mine")) == {"local_date","public_label","service_1_label","service_2_label","response"}
    assert save(client, link, volunteer_id=other["id"]).status_code == 422
    assert client.get(f"/availability/{link}/mine?volunteer_id={other['id']}").status_code == 422
    second = new_link(world.client, open_date(world.client, "2026-11-01"))["token"]
    assert client.get(f"/availability/{second}/mine").status_code == 401
    assert save(client, second).status_code == 401
    for path in ("/staff","/staff/settings","/staff/volunteers",f"/staff/dates/{day['id']}","/staff/audit"):
        assert client.get(path).status_code == 401
    forged = save(client, link, csrf="forged")
    assert forged.status_code == 403
    valid = {"csrf":client.cookies[VOLUNTEER_CSRF],"expected_revision":0,"service_1_available":True,"service_2_available":True}
    assert client.put(f"/availability/{link}/mine",json=valid,headers={"Origin":"https://evil.example"}).status_code == 403
    assert client.put(f"/availability/{link}/mine",json=valid).status_code == 403
    no_pair = client.put(f"/availability/{link}/mine",json={k:v for k,v in valid.items() if k!="service_2_available"},headers={"Origin":"https://testserver"})
    assert no_pair.status_code == 422
    assert detail(client, f"/availability/{link}/mine")["response"]["revision"] == 0


def test_link_csrf_cannot_be_transplanted_or_actor_injected(world):
    person, pin, _, link, client = setup(world)
    other_link = new_link(world.client, open_date(world.client, "2026-11-01"))["token"]
    page = detail(client, f"/availability/{link}")
    data = {"csrf":page["csrf"],"volunteer_id":person["id"],"pin":pin}
    assert client.post(f"/availability/{other_link}/verify-pin",json=data,headers={"Origin":"https://testserver"}).status_code == 403
    assert client.post(f"/availability/{link}/verify-pin",json={**data,"actor_id":world.admin_id},headers={"Origin":"https://testserver"}).status_code == 422
    assert client.post(f"/availability/{link}/verify-pin",json=data,headers={"Origin":"https://evil.example"}).status_code == 403


def test_first_publication_guard_fixture_and_reopen_rules(world):
    person, _, day, link, client = setup(world)
    with world.db.transaction() as c:
        # State guard fixture only, no publication endpoint or publication created.
        c.exec_driver_sql("UPDATE service_dates SET first_published_at=?,availability_open=0,date_input_epoch=date_input_epoch+1 WHERE id=?", (world.now[0], day["id"]))
    assert save(client, link).status_code == 403
    assert client.get(f"/availability/{link}").status_code == 403
    assert post(world.client, f"/staff/dates/{day['id']}/collection", expected_epoch=3, opened=True).status_code == 409
    assert post(world.client, f"/staff/dates/{day['id']}/availability/{person['id']}", expected_revision=0, service_1_available=True, service_2_available=True, confirmation_reason="Confirmed").status_code == 409
    assert post(world.client, f"/staff/dates/{day['id']}/link", expected_epoch=3, operation="rotate").status_code == 409


def test_availability_audit_failure_rolls_back_response_and_epoch(world):
    _, _, day, link, client = setup(world)
    with world.db.transaction() as c:
        c.exec_driver_sql("CREATE TRIGGER stop_response BEFORE INSERT ON audit_events WHEN NEW.action='availability.updated' BEGIN SELECT RAISE(ABORT,'injected'); END")
    assert save(client, link).status_code == 409
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM availability").scalar() == 0
        assert row(c, "SELECT * FROM service_dates WHERE id=?",(day["id"],))["date_input_epoch"] == day["date_input_epoch"]


def test_two_tab_real_sqlite_response_cas(world):
    _, _, day, link, client = setup(world)
    clients = [TestClient(world.app, base_url="https://testserver") for _ in range(2)]
    for other in clients:
        other.cookies.update(client.cookies)
    start = threading.Barrier(2)
    def update(index):
        start.wait(timeout=5)
        return save(clients[index], link, first=bool(index), second=not bool(index))
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(update, (0,1)))
    assert sorted(r.status_code for r in results) == [200,409]
    with world.db.engine.connect() as c:
        assert row(c, "SELECT * FROM availability")["revision"] == 1
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE action='availability.updated'").scalar() == 1
        assert row(c, "SELECT * FROM service_dates")["date_input_epoch"] == day["date_input_epoch"]+1


@pytest.mark.parametrize("winner", ["save","close"])
def test_controlled_save_close_commit_order(world, monkeypatch, winner):
    _, _, day, link, client = setup(world)
    held, release, attempted = threading.Event(), threading.Event(), threading.Event()
    flow = world.app.state.flow
    if winner == "save":
        original = flow.write_response
        def hold(*args, **kwargs):
            held.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(flow,"write_response",hold)
        original_close = flow.collection
        def mark_close(*args):
            attempted.set()
            return original_close(*args)
        monkeypatch.setattr(flow,"collection",mark_close)
    else:
        original = flow.date_cas
        def hold(*args):
            result = original(*args)
            held.set()
            assert release.wait(5)
            return result
        monkeypatch.setattr(flow,"date_cas",hold)
        original_save = flow.save_mine
        def mark_save(*args):
            attempted.set()
            return original_save(*args)
        monkeypatch.setattr(flow,"save_mine",mark_save)
    def close():
        return post(world.client, f"/staff/dates/{day['id']}/collection", expected_epoch=day["date_input_epoch"], opened=False)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(save, client, link) if winner=="save" else pool.submit(close)
        assert held.wait(5)
        second = pool.submit(close) if winner=="save" else pool.submit(save, client, link)
        assert attempted.wait(5)
        release.set()
        result1,result2=first.result(),second.result()
    assert result1.status_code == 200
    assert result2.status_code == (409 if winner=="save" else 403)
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM availability").scalar() == int(winner=="save")
        assert row(c,"SELECT * FROM service_dates")["date_input_epoch"] == day["date_input_epoch"]+1


@pytest.mark.parametrize("day", ["2026-10-26","2026-02-30","20261025","not-a-date"])
def test_reject_non_sunday_dates(world, day):
    sign_in(world)
    post(world.client,"/staff/settings",**settings_payload())
    assert post(world.client,"/staff/dates",local_date=day).status_code == 422
