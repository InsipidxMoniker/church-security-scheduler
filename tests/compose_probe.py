"""Explicit opt-in probe for an EMPTY isolated Compose project, never production."""
import os
import secrets
import sys
import pyotp
from fastapi.testclient import TestClient
from scheduler.app import create_app
from scheduler.auth import Auth
from scheduler.db import Database, row
from scheduler.publication import Publication
from scheduler.security import Security

assert os.environ.get("RELEASE_REHEARSAL") == "synthetic-empty-project-only"
db = Database()
db.check()
if sys.argv[1] == "seed":
    with db.engine.connect() as c:
        count = c.exec_driver_sql("SELECT count(*) FROM admin_users").scalar()
        assert count <= 1 and c.exec_driver_sql("SELECT count(*) FROM schedule_versions").scalar() == 0
    key = Security(os.environ["TOTP_KEY_FILE"])
    auth = Auth(db, key)
    password = secrets.token_urlsafe(32)
    if count:
        with db.engine.connect() as c:
            assert c.exec_driver_sql("SELECT username FROM admin_users").scalar() == "synthetic-rehearsal"
        auth.local_recovery("synthetic-rehearsal", password)
    else:
        auth.bootstrap("synthetic-rehearsal", password)
    app = create_app(db, key, "https://testserver")
    with TestClient(app, base_url="https://testserver") as client:
        # Reuse only test HTTP helpers; no generated secret is printed/saved.
        from conftest import password_login, post
        assert password_login(client, "synthetic-rehearsal", password).status_code == 200
        with db.engine.connect() as c:
            user = row(c, "SELECT * FROM admin_users")
        assert post(client, "/mfa", code=pyotp.TOTP(key.seed(user)).now()).status_code == 200
        from phase2_helpers import settings_payload, open_date
        assert post(client, "/staff/settings", **settings_payload()).status_code == 200
        day = open_date(client)
        path = f"/staff/dates/{day['id']}/draft"
        expected = client.get(path, headers={"Accept":"application/json"}).json()["expected"]
        assert post(client, path + "/generate", **expected).status_code == 200
        expected = client.get(path, headers={"Accept":"application/json"}).json()["expected"]
        assert post(client, path + "/vacancies", **expected, slot_keys=",".join(f"{s}:{p}" for s in (1,2) for p in range(1,9)), reason="Synthetic restart probe").status_code == 200
        expected = client.get(path, headers={"Accept":"application/json"}).json()["expected"]
        assert post(client, path + "/publish", **expected, confirmed=True, idempotency_key="restart-probe").status_code == 200
elif sys.argv[1] != "check":
    raise ValueError("Use seed or check")
with db.engine.connect() as c:
    version = row(c, "SELECT * FROM schedule_versions WHERE status='published'")
    payload = Publication.decode(version)
    assert payload["publication_number"] == 1 and len(payload["slots"]) == 16
    assert c.exec_driver_sql("SELECT current_published_version_id FROM service_dates").scalar() == version["id"]
    assert c.exec_driver_sql("SELECT count(*) FROM vacancy_approvals").scalar() == 16
    assert c.exec_driver_sql("SELECT count(*) FROM staff_sessions WHERE kind='staff' AND revoked_at IS NULL").scalar() == 1
print("Verified persisted publication, hash, pointer, 16 approvals and staff session policy.")
