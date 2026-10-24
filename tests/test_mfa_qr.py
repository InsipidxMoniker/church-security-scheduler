import xml.etree.ElementTree as ET

import pyotp
import pytest
from fastapi.testclient import TestClient

from scheduler.app import PENDING
from scheduler.db import row
from conftest import PASSWORD, password_login, post, sign_in


def test_enrollment_qr_preserves_seed_proof_and_recovery(world):
    assert password_login(world.client).status_code == 200
    before = world.client.get("/mfa", headers={"Accept": "application/json"}).json()
    page = world.client.get("/mfa").text
    assert 'src="/mfa/qr.svg"' in page and before["seed"] in page
    assert "manually" in page and "30-second" in page
    images = [world.client.get("/mfa/qr.svg") for _ in range(2)]
    for image in images:
        assert image.status_code == 200 and image.headers["content-type"] == "image/svg+xml"
        assert ET.fromstring(image.content).tag.endswith("svg")
        assert before["seed"] not in image.text and before["uri"] not in image.text
        assert image.headers["cache-control"] == "no-store"
        assert image.headers["referrer-policy"] == "no-referrer"
        assert image.headers["x-content-type-options"] == "nosniff"
        assert "script-src 'none'" in image.headers["content-security-policy"]
        assert "img-src 'self'" in image.headers["content-security-policy"]
    assert images[0].content == images[1].content
    assert world.client.get("/mfa", headers={"Accept": "application/json"}).json() == before
    with world.db.engine.connect() as c:
        assert not row(c, "SELECT * FROM admin_users WHERE id=?", (world.admin_id,))["enrolled"]
    assert world.client.get("/staff").status_code == 401
    verified = post(world.client, "/mfa", code=pyotp.TOTP(before["seed"]).at(world.now[0]))
    assert verified.status_code == 200 and len(verified.json()["recovery_codes"]) == 10
    assert world.client.get("/mfa/qr.svg").status_code == 401
    assert before["seed"] not in world.client.get("/staff").text


@pytest.mark.parametrize("state", ["missing", "forged", "expired", "revoked", "epoch", "inactive", "staff-cookie-only"])
def test_qr_unauthorized_sessions_never_redirect(world, state):
    if state != "missing":
        password_login(world.client)
    if state == "forged":
        world.client.cookies.clear()
        world.client.cookies.set(PENDING, "forged", domain="testserver.local", path="/")
    elif state == "expired":
        # Loading the QR must not extend the ten-minute absolute deadline.
        world.now[0] += 599
        assert world.client.get("/mfa/qr.svg").status_code == 200
        world.now[0] += 2
    elif state in ("revoked", "epoch", "inactive"):
        with world.db.transaction() as c:
            if state == "revoked":
                world.auth.revoke(c, world.admin_id)
            elif state == "epoch":
                c.exec_driver_sql("UPDATE admin_users SET credential_epoch=credential_epoch+1")
            else:
                c.exec_driver_sql("UPDATE staff_sessions SET user_id=?", (world.scheduler_id,))
                c.exec_driver_sql("UPDATE admin_users SET active=0 WHERE id=?", (world.scheduler_id,))
    elif state == "staff-cookie-only":
        sign_in(world)
    response = world.client.get("/mfa/qr.svg", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 401 and "location" not in response.headers
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"


def test_enrolled_pending_session_cannot_retrieve_seed_or_qr(world):
    codes = sign_in(world).json()["recovery_codes"]
    post(world.client, "/logout")
    password_login(world.client)
    response = world.client.get("/mfa", headers={"Accept": "application/json"})
    assert response.json() == {"enrollment_required": False, "seed": None, "uri": None}
    assert "enrollment-qr" not in world.client.get("/mfa").text
    assert world.client.get("/mfa/qr.svg", follow_redirects=False).status_code == 403
    assert post(world.client, "/mfa", code=codes[0], recovery=True).status_code == 200


def test_reset_qr_only_available_to_reset_account_after_password_login(world):
    sign_in(world)
    target = TestClient(world.app, base_url="https://testserver")
    sign_in(world, "synthetic-scheduler", target)
    with world.db.engine.connect() as c:
        before = world.security.seed(row(c, "SELECT * FROM admin_users WHERE id=?", (world.scheduler_id,)))
    response = post(world.client, f"/staff/users/{world.scheduler_id}/reset", expected_epoch=1, password=PASSWORD + " reset")
    assert response.json() == {"enrollment_required": True}
    assert world.client.get("/mfa/qr.svg?user_id=" + world.scheduler_id).status_code == 401
    assert target.get("/mfa/qr.svg").status_code == 401
    assert password_login(target, "synthetic-scheduler", PASSWORD + " reset").status_code == 200
    data = target.get("/mfa", headers={"Accept": "application/json"}).json()
    assert data["seed"] != before
    assert target.get("/mfa/qr.svg").status_code == 200
    assert post(target, "/mfa", code=pyotp.TOTP(data["seed"]).at(world.now[0])).status_code == 200


def test_account_query_parameters_cannot_select_another_enrollment(world):
    password_login(world.client)
    other = TestClient(world.app, base_url="https://testserver")
    password_login(other, "synthetic-scheduler")
    first = world.client.get("/mfa/qr.svg").content
    second = other.get("/mfa/qr.svg").content
    assert first != second
    assert world.client.get("/mfa/qr.svg?user_id=" + world.scheduler_id + "&seed=attacker").content == first
    assert other.get("/mfa/qr.svg?user_id=" + world.admin_id).content == second
