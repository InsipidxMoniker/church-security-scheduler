"""Deliberate, script-free removal and restoration over real HTTPS/Firefox."""
import pyotp
import pytest
from playwright.sync_api import sync_playwright

from test_auth_health_patch import create_staff, password_form
from test_login_caddy import ORIGIN, stack


def login(page, stack, username="synthetic-browser-admin"):
    page.goto(ORIGIN + "/login")
    password_form(page, username)
    seed = page.locator(".secret").inner_text().strip()
    stack.test_sensitive_values.append(seed)
    page.get_by_label("Authenticator code").fill(pyotp.TOTP(seed).now())
    page.get_by_role("button", name="Verify", exact=True).click()
    page.get_by_role("link", name="I saved my codes — continue").click()


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_staff_delete_cancel_revoke_and_restore(stack, width):
    create_staff(stack, "synthetic-removal-target", "admin")
    with sync_playwright() as playwright, playwright.firefox.launch(headless=True) as browser:
        context = browser.new_context(ignore_https_errors=True, viewport={"width": width, "height": 844})
        victim_context = browser.new_context(ignore_https_errors=True)
        victim = victim_context.new_page()
        login(victim, stack, "synthetic-removal-target")
        page = context.new_page()
        login(page, stack)
        page.goto(ORIGIN + "/staff/users")
        own = page.locator(".account").filter(has=page.get_by_role("heading", name="synthetic-browser-admin", exact=True))
        assert own.get_by_role("link", name="Delete", exact=True).count() == 0
        target = page.locator(".account").filter(has=page.get_by_role("heading", name="synthetic-removal-target", exact=True))
        target.get_by_role("link", name="Delete", exact=True).click()
        dialog = page.get_by_role("dialog")
        assert dialog.get_by_role("heading", name="Delete staff account: synthetic-removal-target?").is_visible()
        assert "deactivate" in dialog.inner_text() and "Historical records will be retained" in dialog.inner_text()
        assert page.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
        dialog.get_by_role("link", name="Cancel", exact=True).click()
        assert target.is_visible()
        target.get_by_role("link", name="Delete", exact=True).click()
        button = page.get_by_role("button", name="Confirm deletion", exact=True)
        button.focus()
        page.keyboard.press("Enter")
        assert "Staff account deactivated" in page.get_by_role("status").inner_text()
        assert target.count() == 0
        assert victim_context.request.get(ORIGIN + "/staff").status == 401
        page.get_by_label("Account status").select_option("inactive")
        page.get_by_role("button", name="Filter accounts", exact=True).click()
        target.get_by_label("Status").select_option("true")
        target.get_by_role("button", name="Save access and revoke sessions", exact=True).click()
        assert target.is_visible()
        assert victim_context.request.get(ORIGIN + "/staff").status == 401
        victim_context.close()
        context.close()


@pytest.mark.parametrize("role,width", [("admin", 1280), ("admin", 390), ("scheduler", 1280), ("scheduler", 390)])
def test_volunteer_delete_cancel_stale_and_restore(stack, role, width):
    username = "synthetic-browser-admin"
    if role == "scheduler":
        username = "synthetic-removal-scheduler"
        create_staff(stack, username, role)
    with sync_playwright() as playwright, playwright.firefox.launch(headless=True) as browser:
        context = browser.new_context(ignore_https_errors=True, viewport={"width": width, "height": 844})
        page = context.new_page()
        login(page, stack, username)
        page.goto(ORIGIN + "/staff/volunteers/new")
        page.get_by_label("Display name", exact=True).fill("Synthetic <Removal> Volunteer")
        page.get_by_role("button", name="Save volunteer", exact=True).click()
        person_url = page.url
        stale = context.new_page()
        stale.goto(person_url + "/delete")
        page.get_by_label("Public label (blank uses name)", exact=True).fill("Synthetic Updated Label")
        page.get_by_role("button", name="Save volunteer", exact=True).click()
        with stale.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/delete")) as result:
            stale.get_by_role("button", name="Confirm deletion", exact=True).click()
        assert result.value.status == 409
        assert "Volunteer changed" in stale.get_by_role("alert").inner_text()
        page.get_by_role("link", name="Delete", exact=True).click()
        dialog = page.get_by_role("dialog")
        assert "Synthetic <Removal> Volunteer (Synthetic Updated Label)" in dialog.inner_text()
        assert "Historical records will be retained" in dialog.inner_text()
        assert page.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
        dialog.get_by_role("link", name="Cancel", exact=True).click()
        assert page.get_by_label("Status").input_value() == "true"
        page.goto(ORIGIN + "/staff/volunteers")
        row = page.locator("article").filter(has_text="Synthetic Updated Label")
        row.get_by_role("link", name="Delete", exact=True).click()
        page.get_by_role("button", name="Confirm deletion", exact=True).click()
        assert "Volunteer deactivated" in page.get_by_role("status").inner_text()
        assert row.count() == 0
        page.get_by_label("Status").select_option("inactive")
        page.get_by_role("button", name="Search roster", exact=True).click()
        row.get_by_role("link", name="Edit", exact=True).click()
        page.get_by_label("Status").select_option("true")
        page.get_by_role("button", name="Save volunteer", exact=True).click()
        page.goto(ORIGIN + "/staff/volunteers")
        assert row.is_visible()
        stale.close()
        context.close()
