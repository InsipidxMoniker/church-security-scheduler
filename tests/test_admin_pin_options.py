"""Custom/random PIN issuance and explicit, audited Administrator viewing."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from conftest import PASSWORD, post, sign_in
from phase2_helpers import create_person, detail, new_link, open_date, save, settings_payload, setup, verify
from scheduler.app import CSRF
from scheduler.auth import Auth
from scheduler.db import Database, migrate, row


@pytest.mark.parametrize("initial_mode,reset_mode", [("custom","custom"),("custom","random"),("random","custom"),("random","random")])
def test_four_digit_custom_and_random_creation_reset_and_view(world, monkeypatch, initial_mode, reset_mode):
    sign_in(world)
    assert post(world.client, "/staff/settings", **settings_payload(pin_digits=4)).status_code == 200
    person = create_person(world.client)
    path = f"/staff/volunteers/{person['id']}"
    choices = iter("0042")
    monkeypatch.setattr("scheduler.volunteer_flow.secrets.choice", lambda alphabet: next(choices))
    first = post(world.client, path + "/pin", expected_revision=1, mode=initial_mode, custom_pin="0012" if initial_mode=="custom" else "")
    assert first.status_code == 200
    pin = first.json()["pin"]
    assert pin == ("0012" if initial_mode=="custom" else "0042")
    assert post(world.client, path + "/pin/view", expected_revision=2).json()["pin"] == pin
    day = open_date(world.client)
    link = new_link(world.client, day)["token"]
    volunteer = TestClient(world.app, base_url="https://testserver")
    assert verify(volunteer, link, person["id"], pin).status_code == 200
    assert save(volunteer, link, first=True, second=True).status_code == 200
    with world.db.engine.connect() as c:
        before = row(c, "SELECT * FROM volunteers WHERE id=?", (person["id"],))
        roster_epoch = c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar()
        secret = row(c, "SELECT * FROM volunteer_pin_secrets")
        assert secret["pin_ciphertext"] != pin
        assert json.loads(world.security.cipher.decrypt(secret["pin_ciphertext"].encode())) == {"purpose":"volunteer-pin-display", "volunteer_id":person["id"], "pin":pin}
        assert before["pin_hash"].startswith("$argon2id$")
    choices = iter("0099")
    reset = post(world.client, path + "/pin", expected_revision=2, mode=reset_mode, custom_pin="0034" if reset_mode=="custom" else "")
    assert reset.status_code == 200
    new_pin = reset.json()["pin"]
    assert new_pin == ("0034" if reset_mode=="custom" else "0099")
    assert volunteer.get(f"/availability/{link}/mine").status_code == 401
    assert verify(volunteer, link, person["id"], pin).status_code == 401
    assert verify(volunteer, link, person["id"], new_pin).status_code == 200
    assert detail(volunteer, f"/availability/{link}/mine")["response"]["choice"] == "both"
    shown = post(world.client, path + "/pin/view", expected_revision=3)
    assert shown.status_code == 200 and shown.json()["pin"] == new_pin
    assert "no-store" in shown.headers["cache-control"]
    assert detail(volunteer, f"/availability/{link}/mine")["response"]["choice"] == "both"
    assert "pin" not in detail(world.client, path)["person"]
    assert "pin_ciphertext" not in str(detail(world.client, path))
    with world.db.engine.connect() as c:
        current = row(c, "SELECT * FROM volunteers WHERE id=?", (person["id"],))
        assert current["revision"] == 3 and current["credential_epoch"] == before["credential_epoch"] + 1
        assert c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar() == roster_epoch
        assert c.exec_driver_sql("SELECT count(*) FROM volunteer_pin_secrets").scalar() == 1
        for event in c.exec_driver_sql("SELECT * FROM audit_events WHERE action IN ('volunteer.pin_issued','volunteer.pin_reset','volunteer.pin_viewed')").mappings():
            details = json.loads(event["details"])
            assert set(details) <= {"revision", "mode"} and event["staff_id"] == world.admin_id
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE action='volunteer.pin_viewed'").scalar() == 2


@pytest.mark.parametrize("values", [
    {"mode":"custom","custom_pin":""}, {"mode":"custom","custom_pin":"123"},
    {"mode":"custom","custom_pin":"1"*13}, {"mode":"custom","custom_pin":"12ab"},
    {"mode":"custom","custom_pin":"１２３４"}, {"mode":"custom","custom_pin":" 1234"},
    {"mode":"custom","custom_pin":1234}, {"mode":"random","custom_pin":"1234"}, {"mode":"other"},
])
def test_invalid_custom_pin_never_changes_credential_or_secret(world, values):
    person, pin, _, link, volunteer = setup(world)
    path = f"/staff/volunteers/{person['id']}"
    with world.db.engine.connect() as c:
        before = row(c, "SELECT * FROM volunteers")
        secret = row(c, "SELECT * FROM volunteer_pin_secrets")
    response = post(world.client, path + "/pin", expected_revision=person["revision"], **values)
    assert response.status_code == 422
    assert verify(volunteer, link, person["id"], pin).status_code == 200
    with world.db.engine.connect() as c:
        assert row(c, "SELECT * FROM volunteers") == before
        assert row(c, "SELECT * FROM volunteer_pin_secrets") == secret
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE action='volunteer.pin_reset'").scalar() == 0


def test_view_role_csrf_origin_and_explicit_action_boundaries(world):
    person, _, _, _, volunteer = setup(world)
    path = f"/staff/volunteers/{person['id']}/pin/view"
    revision = person["revision"]
    scheduler = TestClient(world.app, base_url="https://testserver")
    sign_in(world, "synthetic-scheduler", scheduler)
    assert post(scheduler, path, expected_revision=revision).status_code == 403
    assert "View PIN" not in scheduler.get(path.rsplit("/",2)[0]).text
    assert post(volunteer, path, expected_revision=revision, csrf="forged").status_code == 401
    anonymous = TestClient(world.app, base_url="https://testserver")
    assert post(anonymous, path, expected_revision=revision, csrf="forged").status_code == 401
    assert post(world.client, path, expected_revision=revision, csrf="forged").status_code == 403
    assert world.client.post(path, json={"csrf":world.client.cookies[CSRF],"expected_revision":revision},headers={"Origin":"https://evil.example"}).status_code == 403
    assert world.client.get(path).status_code == 405
    assert post(world.client, path, expected_revision=revision-1).status_code == 409
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE action='volunteer.pin_viewed'").scalar() == 0


def test_legacy_hash_only_pin_and_deactivation_do_not_lose_verification_or_display(world):
    person, pin, _, link, volunteer = setup(world)
    path = f"/staff/volunteers/{person['id']}"
    with world.db.transaction() as c:
        c.exec_driver_sql("DELETE FROM volunteer_pin_secrets")
    missing = world.client.post(path + "/pin/view", data={"csrf":world.client.cookies[CSRF],"expected_revision":person["revision"]}, headers={"Origin":"https://testserver"})
    assert missing.status_code == 409 and "Reset it once" in missing.text
    assert f'href="{path}"' in missing.text
    assert verify(volunteer, link, person["id"], pin).status_code == 200
    reset = post(world.client, path + "/pin", expected_revision=person["revision"], mode="custom", custom_pin="0070")
    assert reset.status_code == 200
    current_revision = reset.json()["revision"]
    for active in (False, True):
        edited = post(world.client, path, expected_revision=current_revision, display_name=person["display_name"], public_label=person["public_label"], active=active, outside_available=False)
        assert edited.status_code == 200
        current_revision = edited.json()["revision"]
        assert post(world.client, path + "/pin/view", expected_revision=current_revision).json()["pin"] == "0070"
    assert verify(volunteer, link, person["id"], "0070").status_code == 200


@pytest.mark.parametrize("damage", ["tamper", "identity", "purpose", "mismatch", "shape"])
def test_saved_pin_tamper_and_substitution_never_disclose_a_pin(world, damage):
    person, _, _, _, _ = setup(world)
    with world.db.transaction() as c:
        stored = row(c, "SELECT * FROM volunteer_pin_secrets")
        claim = json.loads(world.security.cipher.decrypt(stored["pin_ciphertext"].encode()))
        if damage == "identity":
            claim["volunteer_id"] = "another-person"
        elif damage == "purpose":
            claim["purpose"] = "date-link-display"
        elif damage == "mismatch":
            claim["pin"] = "0000"
        elif damage == "shape":
            claim = []
        encrypted = "damaged" if damage=="tamper" else world.security.cipher.encrypt(json.dumps(claim).encode()).decode()
        c.exec_driver_sql("UPDATE volunteer_pin_secrets SET pin_ciphertext=?", (encrypted,))
    response = post(world.client, f"/staff/volunteers/{person['id']}/pin/view", expected_revision=person["revision"])
    assert response.status_code == 409 and set(response.json()) == {"detail"}
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE action='volunteer.pin_viewed'").scalar() == 0


def test_pin_view_and_custom_reset_are_atomic_with_audit(world):
    person, _, _, _, _ = setup(world)
    path = f"/staff/volunteers/{person['id']}"
    with world.db.transaction() as c:
        before = row(c, "SELECT * FROM volunteers")
        secret = row(c, "SELECT * FROM volunteer_pin_secrets")
        c.exec_driver_sql("CREATE TRIGGER stop_pin_events BEFORE INSERT ON audit_events WHEN NEW.action IN ('volunteer.pin_reset','volunteer.pin_viewed') BEGIN SELECT RAISE(ABORT,'injected'); END")
    assert post(world.client, path + "/pin", expected_revision=person["revision"], mode="custom", custom_pin="0091").status_code == 409
    assert post(world.client, path + "/pin/view", expected_revision=person["revision"]).status_code == 409
    with world.db.engine.connect() as c:
        assert row(c, "SELECT * FROM volunteers") == before
        assert row(c, "SELECT * FROM volunteer_pin_secrets") == secret


def test_pin_view_racing_reset_returns_conflict_not_previous_pin(world, monkeypatch):
    person, _, _, _, _ = setup(world)
    path = f"/staff/volunteers/{person['id']}"
    held, release = threading.Event(), threading.Event()
    original = world.security.verify
    def held_verify(*args):
        held.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(world.security,"verify",held_verify)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(post, world.client, path + "/pin/view", expected_revision=person["revision"])
        assert held.wait(5)
        reset = post(world.client, path + "/pin", expected_revision=person["revision"], mode="custom", custom_pin="0081")
        assert reset.status_code == 200
        release.set()
        assert pending.result().status_code == 409
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE action='volunteer.pin_viewed'").scalar() == 0


def test_upgrade_preserves_existing_hash_only_pin(tmp_path, security):
    path = tmp_path / "pin-upgrade.sqlite"
    cfg = Config("alembic.ini")
    cfg.attributes["database_path"] = str(path)
    command.upgrade(cfg,"0003_current_link_display")
    db = Database(path)
    Auth(db,security).bootstrap("synthetic-upgrade-admin",PASSWORD)
    encoded = security.hash_password("0019")
    with db.transaction() as c:
        c.exec_driver_sql("INSERT INTO volunteers(id,display_name,public_label,normalized_label,search_text,active,restricted_qualified,pin_hash) VALUES('synthetic','Synthetic','Synthetic','synthetic','synthetic',1,0,?)",(encoded,))
    db.engine.dispose()
    migrate(path)
    db = Database(path)
    db.check()
    with db.engine.connect() as c:
        assert row(c,"SELECT * FROM volunteers")["pin_hash"] == encoded
        assert c.exec_driver_sql("SELECT count(*) FROM volunteer_pin_secrets").scalar() == 0
    db.engine.dispose()
