"""Real Firefox draft generation/review; all data and keys are disposable."""
import os

import pyotp
from playwright.sync_api import sync_playwright

from scheduler.db import Database
from test_login_caddy import ORIGIN, PASSWORD, stack


def test_mobile_and_desktop_draft_generation_review_and_conflict(stack):
    with sync_playwright() as playwright, playwright.firefox.launch(headless=True) as browser:
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1100, "height": 850})
        page = context.new_page()
        page.goto(ORIGIN + "/login")
        page.get_by_label("Username").fill("synthetic-browser-admin")
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="Continue", exact=True).click()
        page.wait_for_url(ORIGIN + "/mfa")
        seed = page.locator(".secret").inner_text().strip()
        page.get_by_label("Authenticator code").fill(pyotp.TOTP(seed).now())
        page.get_by_role("button", name="Verify", exact=True).click()
        page.get_by_role("link", name="I saved my codes — continue").click()

        def api(path, **values):
            csrf = next(c["value"] for c in context.cookies() if c["name"] == "__Host-csrf")
            response = context.request.post(ORIGIN + path, data={"csrf": csrf, **values}, headers={"Origin": ORIGIN})
            assert response.status == 200, response.text()
            return response.json()

        api("/staff/settings", expected_revision=1, timezone="America/Los_Angeles", service_1_label="First service", service_2_label="Second service",
            essentiality_confirmed=True, **{f"essential_{p}": p != 8 for p in range(1, 9)})
        day = api("/staff/dates", local_date="2026-10-25")
        people = [api("/staff/volunteers", display_name=f"Synthetic draft {i}", public_label="", active=True, restricted_qualified=i < 3) for i in range(7)]
        for person in people:
            api(f"/staff/dates/{day['id']}/availability/{person['id']}", expected_revision=0, service_1_available=True,
                service_2_available=True, confirmation_reason="Confirmed synthetic fixture")
        dashboard = ORIGIN + f"/staff/dates/{day['id']}"
        page.goto(dashboard)
        page.get_by_role("link", name="Schedule draft", exact=True).click()
        assert "No draft exists yet" in page.content()
        page.get_by_role("button", name="Generate draft", exact=True).click()
        assert page.get_by_role("heading", name="Solver result: OPTIMAL", exact=True).is_visible()
        assert page.locator(".schedule-slot").count() == 16
        assert page.get_by_text("VACANT", exact=True).count() == 2
        assert page.get_by_role("region", name="Service 1 assignments").is_visible()
        assert "Sunday canonicalization complete" in page.content()
        columns = page.locator(".services section").evaluate_all("els => els.map(el => el.getBoundingClientRect().x)")
        assert columns[0] < columns[1]
        page.get_by_role("button", name="Regenerate draft", exact=True).click()
        assert "Draft revision 2" in page.content()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        columns = page.locator(".services section").evaluate_all("els => els.map(el => el.getBoundingClientRect().x)")
        assert columns[0] == columns[1]

        # A real migrated DB fixture supplies a lock, without adding Phase 4 UI.
        db = Database(os.environ["DATABASE_PATH"])
        with db.transaction() as c:
            c.exec_driver_sql("UPDATE slots SET locked=1 WHERE service_number=1 AND volunteer_id=?", (people[0]["id"],))
            c.exec_driver_sql("UPDATE schedule_versions SET draft_revision=draft_revision+1 WHERE date_id=?", (day["id"],))
        db.engine.dispose()
        api(f"/staff/dates/{day['id']}/availability/{people[0]['id']}", expected_revision=1, service_1_available=False,
            service_2_available=True, confirmation_reason="Confirmed availability change")
        page.reload()
        assert page.get_by_role("alert").is_visible()
        assert "LOCKED assignment" in page.content()
        page.get_by_role("button", name="Regenerate draft", exact=True).click()
        assert "LOCK_CONFLICT" in page.content()
        page.get_by_role("link", name="Review current form", exact=True).click()
        page.get_by_role("link", name="Schedule draft", exact=True).click()
        assert page.locator(".schedule-slot").count() == 16
        assert "Draft revision 3" in page.content()
        anonymous = browser.new_context(ignore_https_errors=True)
        denied = anonymous.request.get(dashboard + "/draft")
        assert denied.status == 401
        anonymous.close()
        context.close()
