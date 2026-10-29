"""Real Firefox/Caddy publication and amendment journey, desktop and phone."""
import pyotp
from playwright.sync_api import sync_playwright
from test_login_caddy import ORIGIN, PASSWORD, stack


def test_publish_history_signal_pdf_amendment_phone_and_stale_review(stack):
    with sync_playwright() as p, p.firefox.launch(headless=True) as browser:
        context = browser.new_context(ignore_https_errors=True, viewport={"width":1100,"height":850})
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
            response = context.request.post(ORIGIN + path, data={"csrf":csrf, **values}, headers={"Origin":ORIGIN})
            assert response.status == 200, response.text()
            return response.json()
        api("/staff/settings", expected_revision=1, timezone="America/Los_Angeles", service_1_label="First service", service_2_label="Second service", inside_zone_label="INTERIOR UNIQUE", outside_zone_label="EXTERIOR UNIQUE", essentiality_confirmed=True, **{f"essential_{p}":True for p in range(1,9)})
        day = api("/staff/dates", local_date="2026-10-25")
        draft = ORIGIN + f"/staff/dates/{day['id']}/draft"
        page.goto(draft)
        page.get_by_role("button", name="Generate draft", exact=True).click()
        assert "INTERIOR UNIQUE" in page.content() and "EXTERIOR UNIQUE" in page.content()
        other = context.new_page()
        other.goto(draft)
        page.get_by_role("button", name="Regenerate draft", exact=True).click()
        other.get_by_role("button", name="Regenerate draft", exact=True).click()
        assert "changed" in other.content()
        other.close()
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        all_keys = ",".join(f"{s}:{p}" for s in (1,2) for p in range(1,9))
        page.get_by_label("Essential vacancies to approve").fill(all_keys)
        page.get_by_label("Approval reason", exact=True).fill("Private browser confirmation")
        page.get_by_role("button", name="Approve identified vacancies", exact=True).click()
        assert "APPROVED essential vacancy" in page.content()
        page.get_by_role("checkbox").check()
        page.get_by_role("button", name="Confirm publication", exact=True).click()
        page.wait_for_url("**/publications/*")
        first_url = page.url
        assert "CURRENT" in page.content()
        assert "INTERIOR UNIQUE" not in page.content() and "EXTERIOR UNIQUE" not in page.content()
        output = page.get_by_label("Signal-ready text").input_value()
        assert "Version 1" in output and "approved essential vacancy" in output
        assert "Private browser confirmation" not in output
        # Exercise the native manual-copy surface without relaxing script CSP.
        page.get_by_label("Signal-ready text").focus()
        page.keyboard.press("Control+A")
        assert page.get_by_label("Signal-ready text").evaluate("el => el.selectionEnd-el.selectionStart") == len(output)
        with page.expect_download() as download:
            page.get_by_role("link", name="Download PDF", exact=True).click()
        assert download.value.suggested_filename.endswith("-v1.pdf")
        assert download.value.failure() is None
        page.get_by_role("button", name="Create or resume amendment draft").click()
        assert "Not published" in page.content() and "APPROVAL REQUIRED" in page.content()
        page.get_by_label("Essential vacancies to approve").fill(all_keys)
        page.get_by_label("Approval reason", exact=True).fill("Fresh amendment review")
        page.get_by_role("button", name="Approve identified vacancies", exact=True).click()
        page.get_by_role("checkbox").check()
        page.get_by_role("button", name="Confirm publication", exact=True).click()
        assert "Version 2" in page.content() and "AMENDED" in page.content()
        assert "INTERIOR UNIQUE" not in page.content() and "EXTERIOR UNIQUE" not in page.content()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.goto(first_url)
        assert page.get_by_role("alert").is_visible()
        assert "INTERIOR UNIQUE" not in page.content() and "EXTERIOR UNIQUE" not in page.content()
        assert "SUPERSEDED" in page.get_by_label("Signal-ready text").input_value()
        context.close()
