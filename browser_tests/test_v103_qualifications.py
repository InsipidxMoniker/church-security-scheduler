"""Qualification dependency and zone display settings in Firefox over HTTPS."""
import pytest
from playwright.sync_api import sync_playwright

from test_account_removal import login
from test_login_caddy import ORIGIN, stack


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_qualification_dependency_and_zone_labels(stack, width):
    with sync_playwright() as playwright, playwright.firefox.launch(headless=True) as browser:
        context = browser.new_context(ignore_https_errors=True, viewport={"width": width, "height": 844})
        page = context.new_page()
        login(page, stack)
        page.goto(ORIGIN + "/staff/volunteers/new")
        page.get_by_label("Display name", exact=True).fill("Synthetic Rifle Volunteer")
        page.get_by_label("Rifle Qualified", exact=True).select_option("true")
        page.get_by_role("button", name="Save volunteer", exact=True).click()
        assert "Rifle Qualified requires Outside Available" in page.get_by_role("alert").inner_text()
        page.get_by_role("link", name="Review current form", exact=True).click()
        page.get_by_label("Display name", exact=True).fill("Synthetic Rifle Volunteer")
        page.get_by_label("Outside Available", exact=True).select_option("true")
        page.get_by_label("Rifle Qualified", exact=True).select_option("true")
        page.get_by_role("button", name="Save volunteer", exact=True).click()
        person_url = page.url
        assert page.get_by_label("Rifle Qualified", exact=True).input_value() == "true"
        page.get_by_label("Outside Available", exact=True).select_option("false")
        page.get_by_role("button", name="Save volunteer", exact=True).click()
        assert "clear Rifle Qualified first" in page.get_by_role("alert").inner_text()
        page.goto(person_url)
        page.get_by_label("Rifle Qualified", exact=True).select_option("false")
        page.get_by_role("button", name="Save volunteer", exact=True).click()
        page.get_by_label("Outside Available", exact=True).select_option("false")
        page.get_by_role("button", name="Save volunteer", exact=True).click()
        assert page.get_by_label("Outside Available", exact=True).input_value() == "false"
        page.goto(ORIGIN + "/staff/settings")
        page.get_by_label("Church IANA timezone").fill("America/Los_Angeles")
        page.get_by_label("First service label", exact=True).fill("First")
        page.get_by_label("Second service label", exact=True).fill("Second")
        page.get_by_label("Inside zone display label").fill("<Hall>")
        page.get_by_label("Outside zone display label").fill("Grounds")
        page.get_by_role("button", name="Save configuration", exact=True).click()
        assert page.get_by_label("Inside zone display label").input_value() == "<Hall>"
        assert "&lt;Hall&gt;" in page.content()
        assert "Position 3 · Grounds · no qualification required" in page.content()
        assert page.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
        context.close()
