import re
from types import SimpleNamespace

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from scheduler.app import CSRF, create_app
from scheduler.auth import Auth
from scheduler.db import Database, migrate, row
from scheduler.security import Security

PASSWORD = "Synthetic test password only 42"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "migrated.sqlite"
    migrate(path)
    database = Database(path)
    yield database
    database.engine.dispose()


@pytest.fixture
def security(tmp_path):
    path = tmp_path / "external-key"
    path.write_bytes(Fernet.generate_key())
    return Security(path)


@pytest.fixture
def world(db, security):
    now = [1800000000.0]
    auth = Auth(db, security, lambda: now[0])
    admin_id = auth.bootstrap("synthetic-admin", PASSWORD)
    with db.transaction() as c:
        scheduler_id = auth.create_user(c, "synthetic-scheduler", security.hash_password(PASSWORD), "scheduler", admin_id)
    app = create_app(db, security, "https://testserver", lambda: now[0])
    with TestClient(app, base_url="https://testserver") as client:
        yield SimpleNamespace(db=db, security=security, auth=auth, now=now, app=app,
                              client=client, admin_id=admin_id, scheduler_id=scheduler_id)


def post(client, url, **data):
    return client.post(url, json={"csrf": client.cookies.get(CSRF, ""), **data}, headers={"Origin": "https://testserver"})


def password_login(client, username="synthetic-admin", password=PASSWORD):
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    return client.post("/login", json={"csrf": csrf, "username": username, "password": password}, headers={"Origin": "https://testserver"})


def sign_in(world, username="synthetic-admin", client=None):
    client = client or world.client
    assert password_login(client, username).status_code == 200
    with world.db.engine.connect() as c:
        user = row(c, "SELECT * FROM admin_users WHERE username=?", (username,))
    code = pyotp.TOTP(world.security.seed(user)).at(world.now[0])
    result = post(client, "/mfa", code=code)
    assert result.status_code == 200, result.text
    return result
