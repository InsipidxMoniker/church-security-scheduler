"""v1.0.1 response compatibility: HTML is an explicit, page-only opt-in."""
import pytest
from sqlalchemy.exc import OperationalError

from scheduler.app import templates
from scheduler.db import HEAD, audit
from scheduler.security import Problem
from conftest import password_login, sign_in

HTML = {"Accept": "text/html"}
ID = "00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize("path", [
    "/staff", "/staff/users", "/staff/audit", "/staff/health", "/staff/settings",
    "/staff/volunteers", "/staff/volunteers/new", "/staff/volunteers/import",
    f"/staff/volunteers/{ID}", "/staff/dates", f"/staff/dates/{ID}",
    f"/staff/dates/{ID}/draft", f"/staff/dates/{ID}/publications/{ID}", "/mfa",
])
def test_unauthenticated_html_pages_redirect(world, path):
    response = world.client.get(path, headers=HTML, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert "accept" in response.headers["vary"].lower()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("accept,redirect", [
    (None, False), ("*/*", False), ("application/json", False),
    ("text/html,application/json;q=0.1", False),
    ("text/html,application/json;q=0", False),
    ("text/html;q=0", False), ("text/html;q=0.000,*/*;q=1", False),
    ("text/html;q=bogus", False), ("text/html;q=NaN", False),
    ("text/html;q=1.1", False), ("text/html;q=-1", False),
    ("text/html;q=0;q=1", False),
    ("text/html; q = 0", False), ("text/html;q", False),
    ("text/html;q=0.5,*/*;q=1", True), ("text/html;q=1.000", True),
    ("text/plain,text/html; charset=utf-8; q=0.2", True),
])
def test_navigation_negotiation_preserves_json_defaults(world, accept, redirect):
    world.client.headers.pop("accept", None)
    headers = {} if accept is None else {"Accept": accept}
    response = world.client.get("/staff", headers=headers, follow_redirects=False)
    assert response.status_code == (303 if redirect else 401)
    if not redirect:
        assert response.json() == {"detail": "Sign in required"}
        assert "location" not in response.headers


def test_json_content_type_wins_over_html_accept(world):
    response = world.client.get("/staff", headers={**HTML, "Content-Type": "application/json"})
    assert response.status_code == 401 and response.json() == {"detail": "Sign in required"}


def test_root_and_fixed_destination(world):
    response = world.client.get("/?next=https://evil.example&return_to=//evil.example", headers=HTML)
    assert response.status_code == 200 and response.url.path == "/login"
    assert [r.headers["location"] for r in response.history] == ["/staff", "/login"]
    assert password_login(world.client).status_code == 200
    assert world.client.get("/", headers=HTML).url.path == "/login"
    sign_in(world)
    assert world.client.get("/", headers=HTML).url.path == "/staff"


@pytest.mark.parametrize("change", ["idle", "absolute", "revoked", "epoch", "inactive"])
def test_invalid_staff_cookie_redirects_without_loop(world, change):
    sign_in(world)
    with world.db.transaction() as c:
        if change == "revoked":
            world.auth.revoke(c, world.admin_id)
        elif change == "epoch":
            c.exec_driver_sql("UPDATE admin_users SET credential_epoch=credential_epoch+1")
        elif change == "inactive":
            c.exec_driver_sql("UPDATE admin_users SET active=0 WHERE id=?", (world.scheduler_id,))
            # Use a disabled account without violating the last-Administrator guard.
            c.exec_driver_sql("UPDATE staff_sessions SET user_id=?", (world.scheduler_id,))
        else:
            column = "last_used_at" if change == "idle" else "expires_at"
            c.exec_driver_sql(f"UPDATE staff_sessions SET {column}=?", (world.now[0] - 1900,))
    assert world.client.get("/staff", headers=HTML).url.path == "/login"
    assert world.client.get("/staff", headers={"Accept": "application/json"}).status_code == 401


def test_scheduler_permissions_and_mutations_are_not_redirected(world):
    sign_in(world, "synthetic-scheduler")
    for headers in (HTML, {"Accept": "application/json"}):
        response = world.client.get("/staff/users", headers=headers, follow_redirects=False)
        assert response.status_code == 403 and "location" not in response.headers
    world.client.cookies.clear()
    response = world.client.post("/logout", data={"csrf": "forged"}, headers={**HTML, "Origin": "https://testserver"}, follow_redirects=False)
    assert response.status_code == 401 and "location" not in response.headers
    assert world.client.get("/staff", headers={**HTML, "Host": "evil.example"}, follow_redirects=False).status_code == 400
    assert world.client.post("/logout", json={"csrf": "x"}, headers={**HTML, "Origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize("path", [
    "/mfa/qr.svg", f"/staff/dates/{ID}/publications/{ID}/signal.txt",
    f"/staff/dates/{ID}/publications/{ID}/schedule.pdf", "/availability/missing/mine",
])
def test_non_page_endpoints_never_gain_redirects(world, path):
    response = world.client.get(path, headers=HTML, follow_redirects=False)
    assert response.status_code in (401, 404) and "location" not in response.headers


def test_unrelated_401_and_throttling_preserved(world, monkeypatch):
    def reject(*args, **kwargs):
        raise Problem(401, "Invalid credentials")
    monkeypatch.setattr(world.app.state.auth, "session", reject)
    assert world.client.get("/staff", headers=HTML, follow_redirects=False).status_code == 401
    def throttle(*args, **kwargs):
        raise Problem(429, "Retry later", 9)
    monkeypatch.setattr(world.app.state.auth, "session", throttle)
    response = world.client.get("/staff", headers=HTML, follow_redirects=False)
    assert response.status_code == 429 and response.headers["Retry-After"] == "9"


@pytest.mark.parametrize("username", ["synthetic-admin", "synthetic-scheduler"])
def test_health_representations_and_headers(world, username):
    sign_in(world, username)
    expected = {"schema": HEAD, "database": "ready", "version": "1.0.4",
                "backup_status": {"action": "never_recorded"}, "last_successful_backup": None}
    for headers in ({}, {"Accept": "*/*"}, {"Accept": "application/json"}, {"Accept": "text/html,application/json"}):
        assert world.client.get("/staff/health", headers=headers).json() == expected
    world.client.headers.pop("accept", None)
    assert world.client.get("/staff/health").json() == expected
    response = world.client.get("/staff/health", headers=HTML)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert "No application-managed backup recorded." in response.text
    assert "PBS backup status is not monitored" in response.text
    assert HEAD in response.text and "1.0.4" in response.text
    assert "full database integrity or recovery check" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "accept" in response.headers["vary"].lower()


def test_backup_failure_retains_last_success(world):
    sign_in(world)
    with world.db.transaction() as c:
        audit(c, "backup.completed", "database")
    successful = world.client.get("/staff/health").json()
    assert successful["backup_status"]["action"] == "backup.completed"
    assert "Latest application-managed backup completed" in world.client.get("/staff/health", headers=HTML).text
    with world.db.transaction() as c:
        audit(c, "backup.failed", "database")
    response = world.client.get("/staff/health").json()
    assert response["backup_status"]["action"] == "backup.failed"
    assert response["last_successful_backup"] == successful["last_successful_backup"]
    page = world.client.get("/staff/health", headers=HTML).text
    assert "Latest application-managed backup failed" in page
    assert templates.env.filters["utc"](successful["last_successful_backup"]) in page


@pytest.mark.parametrize("failure", ["schema", "database"])
def test_health_failures_are_sanitized_and_json_status_preserved(world, monkeypatch, failure):
    sign_in(world)
    def broken():
        if failure == "schema":
            raise RuntimeError("private-path-and-key-diagnostic")
        raise OperationalError("private SQL", {}, RuntimeError("private-path-and-key-diagnostic"))
    monkeypatch.setattr(world.db, "check", broken)
    html = world.client.get("/staff/health", headers=HTML)
    api = world.client.get("/staff/health", headers={"Accept": "application/json"})
    assert html.status_code == 503 and html.headers["content-type"].startswith("text/html")
    assert api.status_code == (500 if failure == "schema" else 503)
    assert "private-path-and-key-diagnostic" not in html.text + api.text
    assert "private SQL" not in html.text + api.text


def test_health_template_escapes_unknown_values():
    page = templates.env.get_template("health.html").render(health={
        "version": "<script>bad</script>", "database": "<unsafe>", "schema": "<private>",
        "backup_status": {"action": "<img src=x onerror=bad>"}, "last_successful_backup": None,
    })
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "&lt;img" in page
    null = templates.env.get_template("health.html").render(health={
        "version": "1.0.4", "database": "ready", "schema": HEAD,
        "backup_status": None, "last_successful_backup": None,
    })
    assert "No application-managed backup recorded." in null


def test_vary_accept_preserves_other_values(world, monkeypatch):
    sign_in(world)
    original = templates.TemplateResponse
    def varied_template(*args, **kwargs):
        response = original(*args, **kwargs)
        response.headers["Vary"] = "Accept-Encoding"
        return response
    monkeypatch.setattr(templates, "TemplateResponse", varied_template)
    response = world.client.get("/staff/health", headers=HTML)
    assert {part.strip().lower() for part in response.headers["vary"].split(",")} == {"accept", "accept-encoding"}
