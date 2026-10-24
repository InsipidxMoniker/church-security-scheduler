import re
from concurrent.futures import ThreadPoolExecutor

import pyotp
import pytest
from fastapi.testclient import TestClient

from scheduler.app import CSRF, PENDING, STAFF
from scheduler.auth import Auth
from scheduler.db import row
from scheduler.security import Problem, digest, password_policy
from conftest import PASSWORD, password_login, post, sign_in


@pytest.mark.parametrize("path", ["/staff", "/staff/users", "/staff/audit", "/staff/health"])
def test_staff_reads_require_auth(world, path):
    assert world.client.get(path).status_code == 401


def test_bootstrap_one_time_and_password_policy(world):
    with pytest.raises(Problem) as error:
        world.auth.bootstrap("another", PASSWORD)
    assert error.value.status == 409
    for invalid in ("x" * 14, "x" * 257):
        with pytest.raises(Problem):
            password_policy(invalid)
    password_policy(" " * 15)
    password_policy("字" * 256)
    with world.db.engine.connect() as c:
        user = row(c, "SELECT * FROM admin_users WHERE id=?", (world.admin_id,))
        assert "$argon2id$v=19$m=19456,t=2,p=1$" in user["password_hash"]
        assert world.security.seed(user) not in user["totp_secret"]


def test_pending_is_not_privileged_and_mfa_rotates(world):
    assert password_login(world.client).status_code == 200
    pending = world.client.cookies[PENDING]
    assert world.client.get("/staff").status_code == 401
    data = world.client.get("/mfa", headers={"Accept": "application/json"}).json()
    assert data["enrollment_required"] and data["seed"]
    result = post(world.client, "/mfa", code=pyotp.TOTP(data["seed"]).at(world.now[0]))
    assert result.status_code == 200
    assert len(result.json()["recovery_codes"]) == 10
    assert PENDING not in world.client.cookies
    assert world.client.cookies[STAFF] != pending
    assert world.client.get("/staff").status_code == 200
    for value in result.headers.get_list("set-cookie"):
        assert "Secure" in value and "HttpOnly" in value and "SameSite=lax" in value and "Path=/" in value
    with world.db.engine.connect() as c:
        assert row(c, "SELECT * FROM staff_sessions WHERE token_hash=?", (digest(pending),))["revoked_at"]
        stored = str(c.exec_driver_sql("SELECT * FROM recovery_codes").all())
        assert all(code not in stored for code in result.json()["recovery_codes"])


def test_totp_replay_and_clock_skew(world):
    sign_in(world)
    post(world.client, "/logout")
    assert password_login(world.client).status_code == 200
    with world.db.engine.connect() as c:
        user = row(c, "SELECT * FROM admin_users WHERE id=?", (world.admin_id,))
    otp = pyotp.TOTP(world.security.seed(user))
    assert post(world.client, "/mfa", code=otp.at(world.now[0])).status_code == 401
    assert post(world.client, "/mfa", code=otp.at(world.now[0] + 60)).status_code == 401
    assert post(world.client, "/mfa", code=otp.at(world.now[0] + 30)).status_code == 200


def test_recovery_consumed_atomically(world):
    codes = sign_in(world).json()["recovery_codes"]
    first_raw = world.client.cookies[STAFF]
    clients = [TestClient(world.app, base_url="https://testserver") for _ in range(2)]
    for client in clients:
        assert password_login(client).status_code == 200
    def use(client):
        return post(client, "/mfa", code=codes[0], recovery=True).status_code
    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(use, clients))
    assert statuses.count(200) == 1
    assert all(s in (200, 401) for s in statuses)
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM recovery_codes WHERE consumed_at IS NOT NULL").scalar() == 1
        assert row(c, "SELECT * FROM staff_sessions WHERE token_hash=?", (digest(first_raw),))["revoked_at"]


def test_concurrent_totp_reuse(world):
    clients = [TestClient(world.app, base_url="https://testserver") for _ in range(2)]
    for client in clients:
        assert password_login(client).status_code == 200
    with world.db.engine.connect() as c:
        user = row(c, "SELECT * FROM admin_users WHERE id=?", (world.admin_id,))
    code = pyotp.TOTP(world.security.seed(user)).at(world.now[0])
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda client: post(client, "/mfa", code=code).status_code, clients))
    assert sorted(results) == [200, 401]


def test_session_survives_engine_restart_with_same_key(world):
    sign_in(world)
    world.db.engine.dispose()
    from scheduler.app import create_app
    app = create_app(world.db, world.security, "https://testserver", lambda: world.now[0])
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.update(world.client.cookies)
        assert client.get("/staff").status_code == 200


def test_admin_role_change_revokes_target_browser_session(world):
    sign_in(world)
    target = TestClient(world.app, base_url="https://testserver")
    sign_in(world, "synthetic-scheduler", target)
    result = post(world.client, f"/staff/users/{world.scheduler_id}/access", expected_epoch=1, role="admin", active=True)
    assert result.status_code == 200
    assert target.get("/staff").status_code == 401


def test_tunable_throttle_limits(world, monkeypatch):
    monkeypatch.setenv("AUTH_ACCOUNT_LIMIT", "2")
    monkeypatch.setenv("AUTH_ACCOUNT_WINDOW", "10")
    changed = Auth(world.db, world.security, lambda: world.now[0])
    for _ in range(2):
        scope = changed.reserve_attempt("tunable", "test-ip")
        changed.finish_attempt(scope, False)
    with pytest.raises(Problem) as error:
        changed.reserve_attempt("tunable", "another-ip")
    assert error.value.status == 429
    world.now[0] += 11
    changed.reserve_attempt("tunable", "another-ip")


@pytest.mark.parametrize("change", ["logout", "reset", "role", "deactivate", "epoch", "idle", "absolute"])
def test_session_revocation_and_expiry(world, change):
    sign_in(world, "synthetic-scheduler")
    if change == "logout":
        assert post(world.client, "/logout").status_code == 200
    elif change == "reset":
        world.auth.local_recovery("synthetic-scheduler", PASSWORD + " reset")
    elif change == "idle":
        world.now[0] += 1801
    elif change == "absolute":
        with world.db.transaction() as c:
            c.exec_driver_sql("UPDATE staff_sessions SET created_at=?,expires_at=?", (world.now[0] - 30000, world.now[0] - 1))
    else:
        with world.db.transaction() as c:
            if change == "role":
                c.exec_driver_sql("UPDATE admin_users SET role='admin',credential_epoch=credential_epoch+1 WHERE id=?", (world.scheduler_id,))
            elif change == "deactivate":
                c.exec_driver_sql("UPDATE admin_users SET active=0 WHERE id=?", (world.scheduler_id,))
            else:
                c.exec_driver_sql("UPDATE admin_users SET credential_epoch=credential_epoch+1 WHERE id=?", (world.scheduler_id,))
    assert world.client.get("/staff").status_code == 401


def test_current_role_checked_and_scheduler_denied(world):
    sign_in(world, "synthetic-scheduler")
    assert world.client.get("/staff/users").status_code == 403
    assert post(world.client, "/staff/users", username="new", password=PASSWORD, role="admin").status_code == 403
    assert post(world.client, f"/staff/users/{world.admin_id}/reset", expected_epoch=1, password=PASSWORD).status_code == 403
    assert post(world.client, f"/staff/users/{world.admin_id}/access", expected_epoch=1, role="scheduler", active=True).status_code == 403
    assert post(world.client, f"/staff/users/{world.admin_id}/clear-block").status_code == 403
    assert world.client.get("/staff/audit").status_code == 200


def test_last_admin_and_stale_account_change(world):
    sign_in(world)
    for data in [{"role": "scheduler", "active": True}, {"role": "admin", "active": False}]:
        assert post(world.client, f"/staff/users/{world.admin_id}/access", expected_epoch=1, **data).status_code == 409
    url = f"/staff/users/{world.scheduler_id}/access"
    assert post(world.client, url, expected_epoch=1, role="scheduler", active=False).status_code == 200
    assert post(world.client, url, expected_epoch=1, role="admin", active=True).status_code == 409


def test_admin_create_reset_clear_block_and_audit(world):
    sign_in(world)
    result = post(world.client, "/staff/users", username="New Staff", password=PASSWORD, role="scheduler")
    assert result.status_code == 201
    user_id = result.json()["id"]
    assert post(world.client, f"/staff/users/{user_id}/reset", expected_epoch=1, password=PASSWORD + " updated").status_code == 200
    assert post(world.client, f"/staff/users/{user_id}/clear-block").status_code == 200
    events = world.client.get("/staff/audit", headers={"Accept": "application/json"}).json()["events"]
    assert {"staff.created", "staff.credentials_reset", "staff.block_cleared"} <= {e["action"] for e in events}
    assert PASSWORD not in str(events)


@pytest.mark.parametrize("route,data", [
    ("/logout", {}), ("/staff/users", {"username": "new", "password": PASSWORD, "role": "admin"}),
    ("/staff/users/{id}/access", {"expected_epoch": 1, "role": "admin", "active": True}),
    ("/staff/users/{id}/reset", {"expected_epoch": 1, "password": PASSWORD}),
    ("/staff/users/{id}/clear-block", {}),
])
def test_csrf_and_origin_on_every_staff_mutation(world, route, data):
    sign_in(world)
    route = route.format(id=world.admin_id)
    assert world.client.post(route, json={"csrf": world.client.cookies[CSRF], **data}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert post(world.client, route, csrf="forged", **data).status_code == 403
    assert world.client.post(route, json={"csrf": world.client.cookies[CSRF], **data}).status_code == 403


def test_login_mfa_csrf_origin_and_actor_injection(world):
    page = world.client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    data = {"csrf": csrf, "username": "synthetic-admin", "password": PASSWORD}
    assert world.client.post("/login", json=data).status_code == 403
    assert world.client.post("/login", json={**data, "csrf": "fake"}, headers={"Origin": "https://testserver"}).status_code == 403
    assert password_login(world.client).status_code == 200
    assert post(world.client, "/mfa", csrf="fake", code="123456").status_code == 403
    assert world.client.post("/mfa", json={"csrf": world.client.cookies[CSRF], "code": "123456"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert post(world.client, "/mfa", code="123456", role="admin").status_code == 422


def test_throttles_persist_block_before_hash_and_expire(world, monkeypatch):
    for _ in range(5):
        assert password_login(world.client, password="wrong").status_code == 401
    def forbidden(*args):
        pytest.fail("A blocked request must not hash credentials")
    original = world.security.verify
    monkeypatch.setattr(world.security, "verify", forbidden)
    response = password_login(world.client)
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0
    restarted = Auth(world.db, world.security, lambda: world.now[0])
    with pytest.raises(Problem) as error:
        restarted.login("synthetic-admin", PASSWORD, "different-ip")
    assert error.value.status == 429
    monkeypatch.setattr(world.security, "verify", original)
    world.now[0] += 901
    assert password_login(world.client).status_code == 200


def test_mfa_attempts_throttled(world):
    password_login(world.client)
    for _ in range(5):
        assert post(world.client, "/mfa", code="invalid").status_code == 401
    assert post(world.client, "/mfa", code="invalid").status_code == 429


def test_atomic_account_throttle(world):
    def attempt(_):
        try:
            scope = world.auth.reserve_attempt("target", "shared-ip")
            world.auth.finish_attempt(scope, False)
            return 401
        except Problem as exc:
            return exc.status
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert results.count(401) == 5 and results.count(429) == 11


def test_shared_nat_limit_and_spoofed_forwarding(world):
    # Different unknown accounts still share the same real connection's IP quota.
    for i in range(30):
        page = world.client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        result = world.client.post("/login", json={"csrf": csrf, "username": f"unknown-{i}", "password": "wrong"}, headers={"Origin": "https://testserver", "X-Real-IP": f"192.0.2.{i+1}", "X-Forwarded-For": f"192.0.2.{i+1}"})
        assert result.status_code == 401
    assert password_login(world.client, username="unknown-final").status_code == 429
    world.now[0] += 301
    assert password_login(world.client, username="unknown-final").status_code == 401


def test_hash_concurrency_bound(world):
    assert world.security.hash_slots.acquire(False)
    assert world.security.hash_slots.acquire(False)
    try:
        with pytest.raises(Problem) as error:
            world.security.verify(world.security.dummy_hash, PASSWORD)
        assert error.value.status == 429
    finally:
        world.security.hash_slots.release()
        world.security.hash_slots.release()


def test_local_recovery_requires_enrollment_and_clears_sessions(world):
    sign_in(world)
    world.auth.local_recovery("synthetic-admin", PASSWORD + " recovered")
    assert world.client.get("/staff").status_code == 401
    assert password_login(world.client).status_code == 401
    assert password_login(world.client, password=PASSWORD + " recovered").status_code == 200
    assert world.client.get("/mfa", headers={"Accept": "application/json"}).json()["enrollment_required"]


def test_headers_errors_body_limit_and_html_escaping(world):
    response = world.client.get("/login")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "https://" not in response.text  # no external resources
    assert world.client.post("/login", content=b"x" * 1048577, headers={"Origin": "https://testserver"}).status_code == 413
    assert world.client.get("/login", headers={"Host": "evil.example"}).status_code == 400
    sign_in(world)
    created = post(world.client, "/staff/users", username="<script>alert(1)</script>", password=PASSWORD, role="scheduler")
    assert created.status_code == 201
    page = world.client.get("/staff/users").text
    assert "<script>" not in page and "&lt;script&gt;" in page
    invalid = post(world.client, "/staff/users", username="bad", password=PASSWORD, role="root", secret="sensitive-input")
    assert invalid.status_code == 422 and "sensitive-input" not in invalid.text
