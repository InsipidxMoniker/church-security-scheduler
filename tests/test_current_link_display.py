"""Staff redisplay of current shared links; public verification remains digest-based."""
import json

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from conftest import PASSWORD, password_login, post, sign_in
from phase2_helpers import detail, new_link, open_date, save, setup
from scheduler.app import create_app
from scheduler.auth import Auth
from scheduler.db import Database, migrate, row
from scheduler.security import digest


@pytest.mark.parametrize("staff_name", ["synthetic-admin", "synthetic-scheduler"])
def test_current_link_staff_only_redisplay_and_encrypted_storage(world, staff_name):
    _, _, day, link, volunteer = setup(world)
    dashboard = f"/staff/dates/{day['id']}"
    url = "https://testserver/availability/" + link
    staff = world.client
    if staff_name != "synthetic-admin":
        staff = TestClient(world.app, base_url="https://testserver")
        sign_in(world, staff_name, staff)
    for _ in range(2):
        page = staff.get(dashboard)
        assert page.status_code == 200 and f'value="{url}"' in page.text
        assert "no-store" in page.headers["cache-control"]
        data = detail(staff, dashboard)
        assert data["shared_link_url"] == url
        assert "token_ciphertext" not in str(data) and "token_digest" not in str(data)
    anonymous = TestClient(world.app, base_url="https://testserver")
    pending = TestClient(world.app, base_url="https://testserver")
    assert password_login(pending).status_code == 200
    for client in (anonymous, pending, volunteer):
        denied = client.get(dashboard, headers={"Accept": "application/json"})
        assert denied.status_code == 401 and link not in denied.text
    assert link not in str(detail(world.client, "/staff/dates"))
    with world.db.engine.connect() as c:
        stored = row(c, "SELECT * FROM date_link_secrets")
        assert link not in stored["token_ciphertext"]
        claim = json.loads(world.security.cipher.decrypt(stored["token_ciphertext"].encode()))
        assert claim["token"] == link and claim["date_id"] == day["id"]
        assert row(c, "SELECT * FROM date_links")["token_digest"] == digest(link)
        assert c.exec_driver_sql("SELECT date_input_epoch FROM service_dates").scalar() == day["date_input_epoch"]
        assert link not in str(c.exec_driver_sql("SELECT details FROM audit_events").all())


def test_current_link_survives_app_restart(world):
    _, _, day, link, _ = setup(world)
    app = create_app(Database(world.db.path), world.security, "https://testserver", lambda: world.now[0])
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.update(world.client.cookies)
        assert detail(client, f"/staff/dates/{day['id']}")["shared_link_url"].endswith("/" + link)


def test_rotation_and_revocation_remove_previous_display_secrets_atomically(world):
    _, _, day, old, volunteer = setup(world)
    assert save(volunteer, old, first=True, second=True).status_code == 200
    path = f"/staff/dates/{day['id']}"
    current_day = detail(world.client, path)["day"]
    with world.db.engine.connect() as c:
        old_id = row(c, "SELECT * FROM date_links")["id"]
    rotated = new_link(world.client, current_day, "rotate")
    current = detail(world.client, path)
    assert current["shared_link_url"].endswith("/" + rotated["token"])
    assert old not in str(current) and old not in world.client.get(path).text
    assert volunteer.get(f"/availability/{old}/mine").status_code == 404
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM date_link_secrets").scalar() == 1
        assert row(c, "SELECT * FROM date_link_secrets WHERE link_id=?", (old_id,)) is None
        assert row(c, "SELECT * FROM availability")["revision"] == 1
    revoked = post(world.client, path + "/link", expected_epoch=current["day"]["date_input_epoch"], operation="revoke")
    assert revoked.status_code == 200
    assert detail(world.client, path)["shared_link_url"] is None
    assert rotated["token"] not in world.client.get(path).text
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM date_link_secrets").scalar() == 0


@pytest.mark.parametrize("damage", ["tamper", "other_date", "purpose", "wrong_token", "invalid_shape"])
def test_invalid_ciphertext_never_discloses_another_link_and_allows_rotation(world, damage):
    _, _, day, token, _ = setup(world)
    other_day = open_date(world.client, "2026-11-01")
    other = new_link(world.client, other_day)
    with world.db.transaction() as c:
        link = row(c, "SELECT * FROM date_links WHERE date_id=?", (day["id"],))
        secret = row(c, "SELECT * FROM date_link_secrets WHERE link_id=?", (link["id"],))
        if damage == "tamper":
            ciphertext = "invalid-ciphertext"
        elif damage == "other_date":
            ciphertext = c.exec_driver_sql("SELECT token_ciphertext FROM date_link_secrets WHERE link_id!=?", (link["id"],)).scalar()
        else:
            claim = json.loads(world.security.cipher.decrypt(secret["token_ciphertext"].encode()))
            if damage == "purpose":
                claim["purpose"] = "volunteer-verify"
            elif damage == "wrong_token":
                claim["token"] = other["token"]
            else:
                claim["token"] = None
            ciphertext = world.security.cipher.encrypt(json.dumps(claim).encode()).decode()
        c.exec_driver_sql("UPDATE date_link_secrets SET token_ciphertext=? WHERE link_id=?", (ciphertext, link["id"]))
    path = f"/staff/dates/{day['id']}"
    data = detail(world.client, path)
    assert data["shared_link_url"] is None
    page = world.client.get(path)
    assert "recoverable copy is unavailable" in page.text
    assert token not in page.text and other["token"] not in page.text
    # A damaged display copy cannot alter the digest-based authorization path.
    assert world.client.get(f"/availability/{token}").status_code == 200
    rotated = new_link(world.client, data["day"], "rotate")
    assert detail(world.client, path)["shared_link_url"].endswith("/" + rotated["token"])


def test_upgrade_hash_only_link_is_preserved_and_requires_explicit_rotation(tmp_path, security):
    path = tmp_path / "existing.sqlite"
    cfg = Config("alembic.ini")
    cfg.attributes["database_path"] = str(path)
    command.upgrade(cfg, "0002_volunteer_flow")
    db = Database(path)
    actor = Auth(db, security).bootstrap("synthetic-upgrade-admin", PASSWORD)
    token = "synthetic-hash-only-link-token-for-upgrade"
    with db.transaction() as c:
        c.exec_driver_sql("INSERT INTO service_dates(id,local_date,timezone_snapshot) VALUES('existing-date','2026-11-01','UTC')")
        c.exec_driver_sql("INSERT INTO date_links(id,date_id,token_digest,generation,created_at,expires_at,creator) VALUES('existing-link','existing-date',?,1,1,9000000000,?)", (digest(token), actor))
    db.engine.dispose()
    migrate(path)
    db = Database(path)
    db.check()
    with db.engine.connect() as c:
        assert row(c, "SELECT * FROM date_links")["token_digest"] == digest(token)
        assert c.exec_driver_sql("SELECT count(*) FROM date_link_secrets").scalar() == 0
    app = create_app(db, security, "https://testserver")
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get(f"/availability/{token}").status_code == 200


def test_legacy_display_fallback_and_link_audit_failure_preserve_current_secret(world):
    _, _, day, token, _ = setup(world)
    path = f"/staff/dates/{day['id']}"
    with world.db.transaction() as c:
        c.exec_driver_sql("DELETE FROM date_link_secrets")
    legacy = detail(world.client, path)
    assert legacy["shared_link_url"] is None
    assert "Replace it once" in world.client.get(path).text
    assert world.client.get(f"/availability/{token}").status_code == 200
    new = new_link(world.client, legacy["day"], "rotate")
    before = detail(world.client, path)
    with world.db.transaction() as c:
        c.exec_driver_sql("CREATE TRIGGER prevent_link_change BEFORE INSERT ON audit_events WHEN NEW.action IN ('date.link_rotate','date.link_revoke') BEGIN SELECT RAISE(ABORT,'injected'); END")
    for operation in ("rotate", "revoke"):
        assert post(world.client, path + "/link", expected_epoch=before["day"]["date_input_epoch"], operation=operation).status_code == 409
        assert detail(world.client, path)["shared_link_url"].endswith("/" + new["token"])
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM date_link_secrets").scalar() == 1
