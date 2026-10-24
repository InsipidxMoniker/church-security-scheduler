"""Disposable Firefox, real TLS proxy, independent QR decoder; no saved QR files."""
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pyotp
import pytest
import zxingcpp
from PIL import Image
from playwright.sync_api import sync_playwright

from scheduler.auth import Auth
from scheduler.db import Database, audit
from scheduler.security import Security
from test_login_caddy import ORIGIN, PASSWORD, stack


def create_staff(stack, username, role):
    db = Database(stack.test_database)
    security = Security(Path(stack.test_database).parent / "totp.key")
    with db.transaction() as c:
        Auth(db, security).create_user(c, username, security.hash_password(PASSWORD), role)
    db.engine.dispose()


def password_form(page, username):
    page.get_by_label("Username").fill(username)
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Continue", exact=True).click()
    page.wait_for_url(ORIGIN + "/mfa")


@pytest.mark.parametrize("username,width", [
    ("synthetic-browser-admin", 1280), ("élise + <ops>& @church", 390), ("夜" * 100, 360),
], ids=["desktop", "mobile-special-label", "mobile-long-unicode-label"])
def test_firefox_decodes_local_qr_and_completes_enrollment(stack, username, width):
    if username != "synthetic-browser-admin":
        create_staff(stack, username, "scheduler")
    with sync_playwright() as playwright, playwright.firefox.launch(headless=True) as browser:
        context = browser.new_context(ignore_https_errors=True, viewport={"width": width, "height": 844})
        requests = []
        context.on("request", lambda request: requests.append(request.url))
        page = context.new_page()
        page.goto(ORIGIN + "/?next=https://evil.example")
        assert page.url == ORIGIN + "/login"
        password_form(page, username)
        image = page.get_by_role("img", name="Authenticator enrollment QR code")
        image.wait_for(state="visible")
        # Decode the pixels Firefox actually rendered, not an encoder variable.
        result = zxingcpp.read_barcode(Image.open(BytesIO(image.screenshot())))
        assert result is not None
        payload = context.request.get(ORIGIN + "/mfa", headers={"Accept": "application/json"}).json()
        assert result.text == payload["uri"]
        uri = urlsplit(result.text)
        fields = parse_qs(uri.query)
        assert uri.scheme == "otpauth" and uri.netloc == "totp"
        assert unquote(uri.path[1:]) == "Church Security Scheduler:" + username
        assert fields["secret"] == [payload["seed"]]
        assert fields["issuer"] == ["Church Security Scheduler"]
        assert fields.get("algorithm", ["SHA1"]) == ["SHA1"]
        assert fields.get("digits", ["6"]) == ["6"]
        assert fields.get("period", ["30"]) == ["30"]
        assert page.locator(".secret").inner_text().strip() == payload["seed"]
        stack.test_sensitive_values.extend([PASSWORD, payload["seed"], payload["uri"]])
        before = context.request.get(ORIGIN + "/mfa/qr.svg")
        assert before.status == 200 and before.headers["cache-control"] == "no-store"
        assert "script-src 'none'" in before.headers["content-security-policy"]
        page.get_by_label("Authenticator code").fill(pyotp.parse_uri(result.text).now())
        page.get_by_role("button", name="Verify", exact=True).click()
        page.get_by_role("heading", name="Save your recovery codes").wait_for()
        assert page.locator(".codes li").count() == 10
        stack.test_sensitive_values.extend(page.locator(".codes li").all_inner_texts())
        page.get_by_role("link", name="I saved my codes — continue").click()
        assert page.get_by_role("heading", name="Staff workspace").is_visible()
        assert context.request.get(ORIGIN + "/mfa/qr.svg").status == 401
        assert all(urlsplit(url).netloc == urlsplit(ORIGIN).netloc for url in requests)
        assert all(not urlsplit(url).query for url in requests if "/mfa" in urlsplit(url).path)
        page.goto(ORIGIN + "/")
        assert page.url == ORIGIN + "/staff"
        context.close()


@pytest.mark.parametrize("role", ["admin", "scheduler"])
def test_mobile_health_keyboard_navigation_and_expired_session(stack, role):
    username = "synthetic-browser-admin"
    if role == "scheduler":
        username = "synthetic-browser-scheduler"
        create_staff(stack, username, role)
    with sync_playwright() as playwright, playwright.firefox.launch(headless=True) as browser:
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})
        page = context.new_page()
        page.goto(ORIGIN + "/staff/health")
        assert page.url == ORIGIN + "/login"
        password_form(page, username)
        seed = page.locator(".secret").inner_text().strip()
        stack.test_sensitive_values.append(seed)
        page.get_by_label("Authenticator code").fill(pyotp.TOTP(seed).now())
        page.get_by_role("button", name="Verify", exact=True).click()
        page.get_by_role("link", name="I saved my codes — continue").click()
        link = page.get_by_role("link", name="System health", exact=True)
        link.focus()
        page.keyboard.press("Enter")
        page.wait_for_url(ORIGIN + "/staff/health")
        assert page.get_by_role("heading", name="System health", exact=True).is_visible()
        assert page.get_by_text("No application-managed backup recorded.", exact=True).is_visible()
        assert page.get_by_text("PBS backup status is not monitored", exact=False).is_visible()
        assert page.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
        assert context.request.get(ORIGIN + "/staff/health", headers={"Accept": "application/json"}).json()["version"] == "1.0.1"
        if role == "scheduler":
            assert context.request.get(ORIGIN + "/staff/users", headers={"Accept": "text/html"}).status == 403
        db = Database(stack.test_database)
        with db.transaction() as c:
            audit(c, "backup.completed", "database")
            audit(c, "backup.failed", "database")
        page.reload()
        assert page.get_by_text("Latest application-managed backup failed", exact=False).is_visible()
        assert "No successful application-managed backup recorded." not in page.locator("main").inner_text()
        with db.transaction() as c:
            c.exec_driver_sql("UPDATE staff_sessions SET expires_at=0 WHERE kind='staff'")
        db.engine.dispose()
        page.goto(ORIGIN + "/staff/dates")
        assert page.url == ORIGIN + "/login"
        assert context.request.get(ORIGIN + "/staff/health", headers={"Accept": "application/json"}).status == 401
        context.close()
