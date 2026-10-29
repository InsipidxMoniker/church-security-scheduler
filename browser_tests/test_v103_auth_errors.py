"""Desktop/mobile native form correction over disposable Firefox/Caddy HTTPS."""
import pyotp
import pytest
import time
from playwright.sync_api import sync_playwright

from test_login_caddy import ORIGIN, PASSWORD, stack


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_bad_credentials_and_mfa_are_inline_and_retryable(stack, width):
    bad_password = "Synthetic-browser-password-never-echo"
    with sync_playwright() as playwright, playwright.firefox.launch(headless=True) as browser:
        context = browser.new_context(ignore_https_errors=True, viewport={"width": width, "height": 844})
        page = context.new_page()
        page.goto(ORIGIN + "/login")
        for username in ('Unknown <name> & "staff"', "synthetic-browser-admin"):
            page.get_by_label("Username", exact=True).fill(username)
            page.get_by_label("Password", exact=True).fill(bad_password)
            with page.expect_response(lambda r: r.request.method == "POST" and r.url == ORIGIN + "/login") as response:
                page.get_by_role("button", name="Continue", exact=True).click()
            assert response.value.status == 401 and page.url == ORIGIN + "/login"
            assert "Invalid username or password" in page.get_by_role("alert").inner_text()
            assert page.get_by_label("Username", exact=True).input_value() == username
            assert page.get_by_label("Password", exact=True).input_value() == ""
            assert bad_password not in page.content() + page.url
            assert all(c["name"] not in ("__Host-staff", "__Host-pending") for c in context.cookies())
        assert page.get_by_role("heading", name="Staff sign in").is_visible()
        assert page.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Continue", exact=True).click()
        page.wait_for_url(ORIGIN + "/mfa")
        seed = page.locator(".secret").inner_text().strip()
        otp = pyotp.TOTP(seed)
        good_codes = {otp.now(), otp.at(int(time.time())-30), otp.at(int(time.time())+30)}
        bad_code = next(f"{i:06d}" for i in range(10) if f"{i:06d}" not in good_codes)
        page.get_by_label("Authenticator code").fill(bad_code)
        with page.expect_response(lambda r: r.request.method == "POST" and r.url == ORIGIN + "/mfa") as response:
            page.get_by_role("button", name="Verify", exact=True).click()
        assert response.value.status == 401 and page.url == ORIGIN + "/mfa"
        assert "Invalid verification code" in page.get_by_role("alert").inner_text()
        assert page.get_by_label("Authenticator code").input_value() == ""
        assert page.locator(".secret").inner_text().strip() == seed
        assert context.request.get(ORIGIN + "/staff", headers={"Accept": "application/json"}).status == 401
        assert page.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
        page.get_by_label("Authenticator code").fill(otp.now())
        page.get_by_role("button", name="Verify", exact=True).click()
        page.get_by_role("link", name="I saved my codes — continue").click()
        assert page.url == ORIGIN + "/staff"
        stack.test_sensitive_values.extend([bad_password, PASSWORD, seed])
        context.close()
