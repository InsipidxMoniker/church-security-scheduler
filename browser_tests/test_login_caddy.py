"""Real Firefox -> HTTPS Caddy -> HTTP Uvicorn, using disposable synthetic data."""
import re
import ssl
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import httpx
import pyotp
import pytest
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright

from scheduler.auth import Auth
from scheduler.db import Database, migrate
from scheduler.security import Security

ORIGIN = "https://localhost:8443"
PASSWORD = "Disposable browser password 42"


@pytest.fixture
def stack(tmp_path, monkeypatch, request):
    proxy_options = getattr(request, "param", {})
    key = tmp_path / "totp.key"
    key.write_bytes(Fernet.generate_key())
    database = tmp_path / "synthetic.sqlite"
    migrate(database)
    db = Database(database)
    Auth(db, Security(key)).bootstrap("synthetic-browser-admin", PASSWORD)
    db.engine.dispose()

    for name, value in {
        "DATABASE_PATH": str(database), "TOTP_KEY_FILE": str(key),
        "APP_ORIGIN": ORIGIN, "TRUSTED_PROXIES": "127.0.0.1/32",
        "HOME": str(tmp_path),
        "SITE_ADDRESS": "localhost", "PUBLIC_ORIGIN": ORIGIN,
        "XDG_DATA_HOME": str(tmp_path / "caddy-data"),
        "XDG_CONFIG_HOME": str(tmp_path / "caddy-config"),
        "CLOUDFLARED_TRUSTED_PROXIES": proxy_options.get("trusted_proxies", ""),
        **proxy_options.get("environment", {}),
    }.items():
        monkeypatch.setenv(name, value)

    # Exercise the deployment's Caddyfile; only the isolated test's upstream dial
    # changes because both child processes run inside this disposable container.
    config = tmp_path / "Caddyfile"
    config.write_text(Path("deploy/Caddyfile").read_text().replace("app:8000", "127.0.0.1:8000"))
    processes = []
    log_paths = []
    sensitive_values = []
    with ExitStack() as cleanup:
        for command in (
            [sys.executable, "-m", "uvicorn", "scheduler.app:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000", "--no-access-log", "--no-proxy-headers"],
            ["caddy", "run", "--config", str(config)],
        ):
            log_path = tmp_path / f"process-{len(processes)}.log"
            log_paths.append(log_path)
            log_file = cleanup.enter_context(log_path.open("w"))
            process = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT)
            processes.append(process)
            def stop(p=process):
                p.terminate()
                p.wait(timeout=10)
            cleanup.callback(stop)

        # Only this private CA's startup probe ignores verification. All HTTP
        # regression requests below validate the generated CA and localhost name.
        probe_headers = {"CF-Connecting-IP":"203.0.113.254"} if proxy_options.get("trusted_proxies") else {}
        with httpx.Client(verify=False, trust_env=False, timeout=1, headers=probe_headers) as probe:
            for _ in range(150):
                if any(p.poll() is not None for p in processes):
                    pytest.fail("Isolated Caddy/application exited during startup")
                try:
                    if probe.get(ORIGIN + "/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Isolated HTTPS application did not become ready")
        ca = tmp_path / "caddy-data/caddy/pki/authorities/local/root.crt"
        context = ssl.create_default_context(cafile=str(ca))
        with httpx.Client(base_url=ORIGIN, verify=context, trust_env=False, headers=probe_headers) as client:
            client.test_sensitive_values = sensitive_values
            client.test_ssl_context = context
            client.test_database = str(database)
            yield client
    logs = "".join(path.read_text(errors="replace") for path in log_paths)
    # Short numeric PINs must match a whole digit sequence, not an incidental
    # substring of a UTC timestamp. Tokens retain the full substring check.
    if any(secret and (re.search(r"(?<![0-9])" + re.escape(secret) + r"(?![0-9])", logs) if secret.isascii() and secret.isdecimal() else secret in logs) for secret in sensitive_values):
        pytest.fail("A synthetic credential/shared-link token appeared in app/proxy logs")


def login_fields(client):
    page = client.get("/login")
    assert page.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    return {"username": "synthetic-browser-admin", "password": PASSWORD, "csrf": csrf}


def test_firefox_login_mfa_through_caddy(stack):
    with sync_playwright() as playwright, playwright.firefox.launch(headless=True) as browser:
        # This container alone uses an ephemeral self-signed CA; no host trust or
        # existing browser profile/account is changed.
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        submitted = []
        def record(request):
            if request.method == "POST":
                headers = request.headers
                submitted.append({name: headers.get(name) for name in ("origin", "referer", "sec-fetch-site")})
        page.on("request", record)
        response = page.goto(ORIGIN + "/login")
        assert response.headers["referrer-policy"] == "no-referrer"
        page.get_by_label("Username").fill("synthetic-browser-admin")
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/login")) as posted:
            page.get_by_role("button", name="Continue", exact=True).click()
        observed = submitted[-1]
        # Header-only diagnostic; never expose bodies, cookies, seeds or codes.
        assert posted.value.status == 303, observed
        page.wait_for_url(ORIGIN + "/mfa")
        assert page.get_by_role("heading", name="Set up your authenticator").is_visible()
        seed = page.locator(".secret").inner_text().strip()
        page.get_by_label("Authenticator code").fill(pyotp.TOTP(seed).now())
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/mfa")) as verified:
            page.get_by_role("button", name="Verify", exact=True).click()
        assert verified.value.status == 200
        assert page.get_by_role("heading", name="Save your recovery codes").is_visible()
        assert page.locator(".codes li").count() == 10
        page.get_by_role("link", name="I saved my codes — continue").click()
        assert page.get_by_role("heading", name="Staff workspace").is_visible()
        assert all(h["sec-fetch-site"] == "same-origin" and h["referer"] is None for h in submitted)
        assert all(h["origin"] in (None, "null", ORIGIN) for h in submitted)
        print("Firefox form headers (non-secret):", submitted)
        context.close()


def test_explicit_public_origin_login_through_caddy(stack):
    # The app sees HTTP upstream; acceptance must use configured public HTTPS.
    result = stack.post("/login", data=login_fields(stack), headers={"Origin": ORIGIN})
    assert result.status_code == 303
    assert result.headers["location"] == "/mfa"
    assert stack.get("/mfa").status_code == 200


@pytest.mark.parametrize("origin", [None, "null"])
def test_private_browser_origin_and_csrf_through_caddy(stack, origin):
    data = login_fields(stack)
    headers = {"Sec-Fetch-Site": "same-origin"}
    if origin:
        headers["Origin"] = origin
    assert stack.post("/login", data={**data, "csrf": "forged"}, headers=headers).status_code == 403
    assert stack.post("/login", data=data, headers=headers).status_code == 303


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "null"},
    {"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://app:8000"},
    {"Origin": "null", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "https://localhost:9443"},
])
def test_untrusted_login_submission_through_caddy(stack, headers):
    assert stack.post("/login", data=login_fields(stack), headers=headers).status_code == 403
