import re

import pyotp
import pytest

from scheduler.app import CSRF
from conftest import PASSWORD


def fields(client):
    result = client.get("/login")
    return {"username": "synthetic-admin", "password": PASSWORD,
            "csrf": re.search(r'name="csrf" value="([^"]+)"', result.text)[1]}


@pytest.mark.parametrize("headers", [
    {"Origin": "https://testserver"},
    {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "same-origin", "Referer": "https://testserver/login"},
])
def test_valid_browser_form_login(world, headers):
    result = world.client.post("/login", data=fields(world.client), headers=headers)
    assert result.status_code == 200  # TestClient follows the 303 to /mfa.
    assert result.url.path == "/mfa"
    assert "Set up your authenticator" in result.text
    assert world.client.get("/staff").status_code == 401


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "null"}, {"Origin": ""},
    {"Origin": "https://evil.example"},
    {"Origin": "https://testserver.evil.example", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin", "Referer": "https://testserver/login"},
    {"Origin": "null", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "null", "Sec-Fetch-Site": "same-site"},
    {"Origin": "null", "Sec-Fetch-Site": "none"},
    {"Origin": "null", "Sec-Fetch-Site": "same-origin", "Referer": "https://evil.example/login"},
    {"Sec-Fetch-Site": "same-origin", "Referer": "https://testserver@evil.example/login"},
    {"Sec-Fetch-Site": "same-origin", "Referer": "http://testserver/login"},
    {"Referer": "https://testserver/login"},  # Not sufficient without Origin/Fetch Metadata.
    {"Origin": "https://testserver", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://app:8000", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "testserver"},
    {"Origin": "https://testserver:9443"},
    {"Origin": "https://testserver https://evil.example"},
])
def test_missing_invalid_or_cross_origin_rejected(world, headers):
    result = world.client.post("/login", data=fields(world.client), headers=headers)
    assert result.status_code == 403
    assert result.json() == {"detail": "Same-origin request required"}


@pytest.mark.parametrize("headers", [
    [("Origin", "https://testserver"), ("Origin", "https://evil.example")],
    [("Origin", "null"), ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "cross-site")],
])
def test_ambiguous_provenance_headers_rejected(world, headers):
    assert world.client.post("/login", data=fields(world.client), headers=headers).status_code == 403


@pytest.mark.parametrize("origin", [None, "null"])
def test_privacy_fallback_still_requires_login_and_mfa_csrf(world, origin):
    headers = {"Sec-Fetch-Site": "same-origin"}
    if origin:
        headers["Origin"] = origin
    data = fields(world.client)
    assert world.client.post("/login", data={**data, "csrf": "forged"}, headers=headers).status_code == 403
    assert world.client.post("/login", data=data, headers=headers).url.path == "/mfa"
    csrf = world.client.cookies[CSRF]
    response = world.client.get("/mfa", headers={"Accept": "application/json"})
    code = pyotp.TOTP(response.json()["seed"]).at(world.now[0])
    assert world.client.post("/mfa", data={"csrf": "forged", "code": code}, headers=headers).status_code == 403
    result = world.client.post("/mfa", data={"csrf": csrf, "code": code}, headers=headers)
    assert result.status_code == 200 and "Save your recovery codes" in result.text
    assert world.client.get("/staff").status_code == 200
    # Cookie-authenticated staff mutations retain token enforcement as well.
    assert world.client.post("/logout", data={"csrf": "forged"}, headers=headers).status_code == 403
    assert world.client.post("/logout", data={"csrf": world.client.cookies[CSRF]}, headers=headers).url.path == "/login"
