"""Explicit browser authentication errors, with unchanged API/security contracts."""
import html
import re
import time

import pyotp
import pytest

from conftest import PASSWORD, password_login, post, sign_in
from scheduler.app import CSRF, PENDING, PREAUTH, STAFF
from scheduler.db import row
from scheduler.security import digest

HTML = {"Accept": "text/html", "Origin": "https://testserver"}
BAD_PASSWORD = "SYNTHETIC-password-never-echo-42"


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


def browser_login(world, username="synthetic-admin", password=BAD_PASSWORD, response=None, headers=None, **fields):
    response = response if response is not None else world.client.get("/login")
    return world.client.post("/login", data={"csrf": csrf(response), "username": username, "password": password, **fields},
                             headers=headers or HTML, follow_redirects=False)


def session_counts(world):
    with world.db.engine.connect() as c:
        return {kind: c.exec_driver_sql("SELECT count(*) FROM staff_sessions WHERE kind=? AND revoked_at IS NULL", (kind,)).scalar()
                for kind in ("pending", "staff")}


@pytest.mark.parametrize("username", ["synthetic-admin", "unknown-account", 'Unknown <script> & "name"', "synthetic-scheduler"])
def test_bad_credentials_are_generic_styled_safe_and_create_no_sessions(world, username, caplog):
    if username == "synthetic-scheduler":
        with world.db.transaction() as c:
            c.exec_driver_sql("UPDATE admin_users SET active=0 WHERE id=?", (world.scheduler_id,))
    response = browser_login(world, username)
    assert response.status_code == 401 and response.headers["content-type"].startswith("text/html")
    assert "Invalid username or password. Try again." in response.text
    assert '<aside id="login-error" role="alert">' in response.text
    assert 'href="/static/app.css"' in response.text and 'action="/login"' in response.text
    value = re.search(r'<input[^>]*name="username"[^>]*value="([^"]*)"', response.text)[1]
    assert html.unescape(value) == username
    assert "<script>" not in response.text
    password_input = re.search(r'<input[^>]*name="password"[^>]*>', response.text)[0]
    assert "value=" not in password_input and 'aria-describedby="login-error"' in password_input
    assert BAD_PASSWORD not in response.text + str(response.url) + caplog.text
    assert session_counts(world) == {"pending": 0, "staff": 0}
    assert PENDING not in world.client.cookies and STAFF not in world.client.cookies
    assert "location" not in response.headers and response.headers["cache-control"] == "no-store"
    assert "accept" in response.headers["vary"].lower()
    assert world.client.get("/staff").status_code == 401


@pytest.mark.parametrize("accept", [None, "*/*", "application/json", "text/html,application/json;q=0", "text/html;q=0", "text/html;q=bogus"])
def test_form_api_errors_keep_json_for_non_html_or_explicit_json(world, accept):
    headers = {"Origin": "https://testserver"}
    world.client.headers.pop("accept", None)
    if accept is not None:
        headers["Accept"] = accept
    response = browser_login(world, headers=headers)
    assert response.status_code == 401 and response.json() == {"detail": "Invalid credentials"}
    assert "location" not in response.headers and session_counts(world) == {"pending": 0, "staff": 0}


def test_json_content_type_precedes_html_accept_on_login_and_mfa(world):
    page = world.client.get("/login")
    response = world.client.post("/login", json={"csrf": csrf(page), "username": "synthetic-admin", "password": BAD_PASSWORD}, headers=HTML)
    assert response.status_code == 401 and response.json() == {"detail": "Invalid credentials"}
    assert session_counts(world) == {"pending": 0, "staff": 0}
    assert password_login(world.client).status_code == 200
    response = world.client.post("/mfa", json={"csrf": world.client.cookies[CSRF], "code": "invalid-synthetic-code"}, headers=HTML)
    assert response.status_code == 401 and response.json() == {"detail": "Invalid credentials"}
    assert session_counts(world) == {"pending": 1, "staff": 0}


def test_login_html_throttle_retains_retry_after_and_blocks_before_hash(world, monkeypatch):
    world.app.state.auth.account_limit = 2
    response = browser_login(world)
    response = browser_login(world, response=response)
    assert response.status_code == 401
    def forbidden(*args):
        pytest.fail("Blocked credentials must not be hashed")
    monkeypatch.setattr(world.security, "verify", forbidden)
    response = browser_login(world, password=PASSWORD, response=response)
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0
    assert response.headers["content-type"].startswith("text/html") and 'role="alert"' in response.text
    assert "Too many authentication attempts" in response.text
    assert PASSWORD not in response.text and session_counts(world) == {"pending": 0, "staff": 0}
    api = password_login(world.client)
    assert api.status_code == 429 and int(api.headers["Retry-After"]) > 0
    assert api.json() == {"detail": "Too many authentication attempts; retry later"}


@pytest.mark.parametrize("damage", ["csrf", "preauth", "expired"])
def test_invalid_login_csrf_is_rejected_with_a_fresh_retry_form(world, damage):
    page = world.client.get("/login")
    old_csrf = csrf(page)
    if damage == "preauth":
        world.client.cookies.delete(PREAUTH)
    elif damage == "expired":
        old_csrf = world.security.cipher.encrypt_at_time(b"synthetic-preauth", int(time.time()) - 601).decode()
        cookie_domain = next(c.domain for c in world.client.cookies.jar if c.name == PREAUTH)
        world.client.cookies.delete(PREAUTH)
        world.client.cookies.set(PREAUTH, old_csrf, domain=cookie_domain, path="/")
    response = browser_login(world, password=PASSWORD, response=page, csrf="forged" if damage == "csrf" else old_csrf)
    assert response.status_code == 403 and session_counts(world) == {"pending": 0, "staff": 0}
    assert csrf(response) != old_csrf
    response = browser_login(world, password=PASSWORD, response=response)
    assert response.status_code == 303 and response.headers["location"] == "/mfa"
    assert session_counts(world) == {"pending": 1, "staff": 0}


def test_cross_origin_form_still_rejected_before_authentication(world):
    response = browser_login(world, password=PASSWORD, headers={"Accept": "text/html", "Origin": "https://evil.example"})
    assert response.status_code == 403 and response.json() == {"detail": "Same-origin request required"}
    assert session_counts(world) == {"pending": 0, "staff": 0}


def test_unsafe_username_is_not_repopulated_on_invalid_form(world):
    response = browser_login(world, username="unsafe\nusername")
    assert response.status_code == 422 and 'name="username"' in response.text
    assert "unsafe" not in response.text and session_counts(world) == {"pending": 0, "staff": 0}


@pytest.mark.parametrize("enrolled", [False, True])
@pytest.mark.parametrize("recovery", [False, True])
def test_bad_mfa_code_preserves_pending_session_and_allows_retry(world, enrolled, recovery, caplog):
    if enrolled:
        sign_in(world)
        assert post(world.client, "/logout").status_code == 200
        world.now[0] += 30
    assert password_login(world.client).status_code == 200
    pending = world.client.cookies[PENDING]
    with world.db.engine.connect() as c:
        user = row(c, "SELECT * FROM admin_users WHERE id=?", (world.admin_id,))
    seed = world.security.seed(user)
    # Text cannot accidentally match a six-digit OTP; direct form tests also
    # cover recovery-code submissions without printing their value back.
    bad_code = "synthetic-recovery-secret" if recovery else "synthetic-invalid-otp"
    response = world.client.post("/mfa", data={"csrf": world.client.cookies[CSRF], "code": bad_code, "recovery": str(recovery).lower()}, headers=HTML)
    assert response.status_code == 401 and response.headers["content-type"].startswith("text/html")
    assert "Invalid verification code. Try again." in response.text and 'role="alert"' in response.text
    assert bad_code not in response.text + str(response.url) + caplog.text
    assert world.client.cookies[PENDING] == pending and STAFF not in world.client.cookies
    assert session_counts(world) == {"pending": 1, "staff": 0}
    if enrolled:
        assert seed not in response.text and "secret" not in response.text
        if recovery:
            assert re.search(r'<details\s+open', response.text)
    else:
        assert seed in response.text  # authorized, existing enrollment presentation
    response = world.client.post("/mfa", data={"csrf": world.client.cookies[CSRF], "code": pyotp.TOTP(seed).at(world.now[0])}, headers=HTML, follow_redirects=False)
    if enrolled:
        assert response.status_code == 303 and response.headers["location"] == "/staff"
    else:
        assert response.status_code == 200  # existing one-time recovery-code page
    assert STAFF in world.client.cookies and PENDING not in world.client.cookies
    with world.db.engine.connect() as c:
        assert row(c, "SELECT * FROM staff_sessions WHERE token_hash=?", (digest(pending),))["revoked_at"] is not None


def test_mfa_html_throttle_and_expiry_keep_security_boundary(world):
    assert password_login(world.client).status_code == 200
    world.app.state.auth.account_limit = 2
    for _ in range(2):
        assert world.client.post("/mfa", data={"csrf": world.client.cookies[CSRF], "code": "invalid"}, headers=HTML).status_code == 401
    blocked = world.client.post("/mfa", data={"csrf": world.client.cookies[CSRF], "code": "invalid"}, headers=HTML)
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    assert 'role="alert"' in blocked.text and session_counts(world) == {"pending": 1, "staff": 0}
    world.now[0] += 601
    expired = world.client.post("/mfa", data={"csrf": world.client.cookies[CSRF], "code": "invalid"}, headers=HTML)
    assert expired.status_code == 401 and "Your sign-in expired" in expired.text
    assert 'action="/login"' in expired.text and "Manual setup secret" not in expired.text
    assert STAFF not in world.client.cookies
