"""Phase 2 staff and mobile volunteer journey through real HTTPS Caddy."""
import pyotp
from playwright.sync_api import sync_playwright

from test_login_caddy import ORIGIN, PASSWORD, stack


def test_firefox_staff_setup_and_mobile_availability(stack):
    with sync_playwright() as playwright, playwright.firefox.launch(headless=True) as browser:
        staff_context = browser.new_context(ignore_https_errors=True)
        staff = staff_context.new_page()
        staff.goto(ORIGIN + "/login")
        staff.get_by_label("Username").fill("synthetic-browser-admin")
        staff.get_by_label("Password",exact=True).fill(PASSWORD)
        staff.get_by_role("button",name="Continue",exact=True).click()
        staff.wait_for_url(ORIGIN+"/mfa")
        seed=staff.locator(".secret").inner_text().strip()
        staff.get_by_label("Authenticator code").fill(pyotp.TOTP(seed).now())
        staff.get_by_role("button",name="Verify",exact=True).click()
        staff.get_by_role("link",name="I saved my codes — continue").click()

        staff.get_by_role("link",name="System configuration",exact=True).click()
        staff.get_by_label("Church IANA timezone").fill("America/Los_Angeles")
        staff.get_by_label("First service label",exact=True).fill("First service")
        staff.get_by_label("Second service label",exact=True).fill("Second service")
        staff.get_by_label("Classification confirmation").select_option("true")
        staff.get_by_role("button",name="Save configuration",exact=True).click()
        staff.get_by_role("link",name="Staff workspace",exact=True).click()
        assert "Setup incomplete" not in staff.content()

        staff.get_by_role("link",name="Volunteer roster",exact=True).click()
        staff.get_by_role("link",name="Add volunteer",exact=True).click()
        staff.get_by_label("Display name",exact=True).fill("Synthetic Browser Volunteer")
        staff.get_by_label("Positions 1 and 2 qualification").select_option("true")
        staff.get_by_role("button",name="Save volunteer",exact=True).click()
        staff.get_by_role("button",name="Issue PIN",exact=True).click()
        pin=staff.get_by_test_id("generated-pin").inner_text().strip()
        assert len(pin)==8 and pin.isdecimal()
        person_url=ORIGIN+staff.get_by_role("link",name="Return to volunteer",exact=True).get_attribute("href")
        staff.get_by_role("link",name="Return to volunteer",exact=True).click()
        assert pin not in staff.content()

        staff.goto(ORIGIN+"/staff/dates")
        staff.get_by_label("Sunday date").fill("2026-11-01")  # DST boundary, calendar date preserved.
        staff.get_by_role("button",name="Open Sunday",exact=True).click()
        dashboard=staff.url
        assert staff.get_by_text("No response",exact=True).is_visible()
        staff.get_by_role("button",name="Apply link action",exact=True).click()
        shared=staff.get_by_test_id("shared-link").input_value()
        stack.test_sensitive_values.extend([pin,shared.rsplit("/",1)[-1]])
        staff.get_by_role("link",name="Return to Sunday",exact=True).click()
        assert staff.get_by_label("Current shared link",exact=True).input_value()==shared
        staff.reload()
        assert staff.get_by_label("Current shared link",exact=True).input_value()==shared

        context=browser.new_context(ignore_https_errors=True, viewport={"width":390,"height":844},has_touch=True)
        volunteer=context.new_page()
        paths=[]
        volunteer.on("request",lambda request: paths.append(request.url))
        public=volunteer.goto(shared)
        assert public.status==200
        volunteer.get_by_label("Your name",exact=True).select_option(label="Synthetic Browser Volunteer")
        wrong="11111111" if pin!="11111111" else "22222222"
        stack.test_sensitive_values.append(wrong)
        volunteer.get_by_label("Personal PIN",exact=True).fill(wrong)
        volunteer.get_by_role("button",name="Verify my identity",exact=True).click()
        assert "Identity or PIN could not be verified" in volunteer.content()
        volunteer.get_by_role("link",name="Review current form",exact=True).click()
        volunteer.get_by_label("Your name",exact=True).select_option(label="Synthetic Browser Volunteer")
        volunteer.get_by_label("Personal PIN",exact=True).fill(pin)
        volunteer.get_by_role("button",name="Verify my identity",exact=True).click()
        volunteer.wait_for_url(shared+"/mine")
        assert "You have not responded yet" in volunteer.content()
        assert volunteer.locator('input[name="choice"]:checked').count()==0
        volunteer.get_by_label("Both services",exact=True).check()
        volunteer.get_by_role("button",name="Save availability",exact=True).click()
        assert "Availability saved for 2026-11-01" in volunteer.get_by_role("status").inner_text()
        assert "Both services" in volunteer.get_by_role("status").inner_text()
        assert volunteer.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert all(path.startswith(ORIGIN+"/") for path in paths)

        staff.goto(person_url)
        staff.get_by_label("PIN option",exact=True).select_option("custom")
        staff.get_by_label("Custom PIN",exact=True).fill("0042")
        staff.get_by_role("button",name="Reset PIN",exact=True).click()
        assert staff.get_by_test_id("generated-pin").inner_text().strip()=="0042"
        stack.test_sensitive_values.append("0042")
        staff.get_by_role("link",name="Return to volunteer",exact=True).click()
        staff.get_by_role("button",name="View PIN",exact=True).click()
        assert staff.get_by_test_id("generated-pin").inner_text().strip()=="0042"
        staff.get_by_role("link",name="Return to volunteer",exact=True).click()
        denied=volunteer.goto(shared+"/mine")
        assert denied.status==401
        volunteer.get_by_role("link",name="Review current form",exact=True).click()
        volunteer.get_by_label("Your name",exact=True).select_option(label="Synthetic Browser Volunteer")
        volunteer.get_by_label("Personal PIN",exact=True).fill(pin)
        volunteer.get_by_role("button",name="Verify my identity",exact=True).click()
        assert "Identity or PIN could not be verified" in volunteer.content()
        volunteer.get_by_role("link",name="Review current form",exact=True).click()
        volunteer.get_by_label("Your name",exact=True).select_option(label="Synthetic Browser Volunteer")
        volunteer.get_by_label("Personal PIN",exact=True).fill("0042")
        volunteer.get_by_role("button",name="Verify my identity",exact=True).click()
        volunteer.wait_for_url(shared+"/mine")
        assert volunteer.get_by_label("Both services",exact=True).is_checked()

        stale=context.new_page()
        stale.goto(shared+"/mine")
        volunteer.get_by_label("First service only",exact=True).check()
        volunteer.get_by_role("button",name="Save availability",exact=True).click()
        stale.get_by_label("Neither service",exact=True).check()
        with stale.expect_response(lambda r:r.request.method=="POST" and r.url.endswith("/mine")) as conflicted:
            stale.get_by_role("button",name="Save availability",exact=True).click()
        assert conflicted.value.status==409
        assert "Availability changed in another window" in stale.content()
        stale.get_by_role("link",name="Review current form").click()
        assert stale.get_by_label("First service only",exact=True).is_checked()

        staff.goto(dashboard)
        staff.get_by_role("button",name="Close collection",exact=True).click()
        with volunteer.expect_response(lambda r:r.request.method=="POST" and r.url.endswith("/mine")) as closed:
            volunteer.get_by_role("button",name="Save availability",exact=True).click()
        assert closed.value.status==403 and "Collection is closed" in volunteer.content()
        staff.get_by_role("button",name="Reopen collection",exact=True).click()
        volunteer.goto(shared+"/mine")
        assert volunteer.get_by_label("First service only",exact=True).is_checked()

        staff.get_by_label("Link action").select_option("rotate")
        staff.get_by_role("button",name="Apply link action",exact=True).click()
        replacement=staff.get_by_test_id("shared-link").input_value()
        stack.test_sensitive_values.append(replacement.rsplit("/",1)[-1])
        assert replacement!=shared
        staff.get_by_role("link",name="Return to Sunday",exact=True).click()
        staff.reload()
        assert staff.get_by_label("Current shared link",exact=True).input_value()==replacement
        assert shared not in staff.content()
        denied=volunteer.goto(shared+"/mine")
        assert denied.status==404 and "Link unavailable" in volunteer.content()
        volunteer.goto(replacement)
        assert volunteer.get_by_label("Personal PIN",exact=True).is_visible()
        context.close()
        staff_context.close()
