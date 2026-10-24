"""Real TLS Caddy -> Uvicorn -> persisted SQLite throttle integration.

127.0.0.1 simulates the explicitly trusted cloudflared connection. 127.0.0.2
is a distinct actual TCP peer, not an XFF-based simulated address. No echo/IP
diagnostic endpoint, real Tunnel, public credentials or access logging is used.
"""
import re
import sqlite3
import uuid
from contextlib import closing

import httpx
import pyotp
import pytest

from scheduler.security import digest
from test_login_caddy import ORIGIN, PASSWORD, login_fields, stack

TUNNEL = {"trusted_proxies":"127.0.0.1/32 ::1/128"}


def client(stack, visitor_ip, peer="127.0.0.1"):
    transport = httpx.HTTPTransport(verify=stack.test_ssl_context, local_address=peer, trust_env=False)
    return httpx.Client(base_url=ORIGIN, transport=transport, trust_env=False,
                        headers={"CF-Connecting-IP":visitor_ip}, timeout=5)


def fixture_data(stack):
    response = stack.post("/login", data=login_fields(stack), headers={"Origin":ORIGIN})
    assert response.status_code == 303
    seed = stack.get("/mfa", headers={"Accept":"application/json"}).json()["seed"]
    stack.test_sensitive_values.extend([PASSWORD, seed])
    assert stack.post("/mfa", json={"csrf":stack.cookies["__Host-csrf"],"code":pyotp.TOTP(seed).now()}, headers={"Origin":ORIGIN}).status_code == 200
    def api(path, **values):
        response = stack.post(path, json={"csrf":stack.cookies["__Host-csrf"],**values}, headers={"Origin":ORIGIN})
        assert response.status_code == 200
        return response.json()
    api("/staff/settings",expected_revision=1,timezone="America/Los_Angeles",service_1_label="First",service_2_label="Second",essentiality_confirmed=True,**{f"essential_{i}":True for i in range(1,9)})
    person = api("/staff/volunteers",display_name="Synthetic proxy test",active=True,restricted_qualified=True)
    pin = api(f"/staff/volunteers/{person['id']}/pin",expected_revision=person["revision"])["pin"]
    day = api("/staff/dates",local_date="2026-11-01")
    token = api(f"/staff/dates/{day['id']}/link",expected_epoch=day["date_input_epoch"],operation="create")["token"]
    stack.test_sensitive_values.extend([pin, token])
    return person["id"], pin, token


def attempt(visitor, link, person=None, pin="wrong"):
    response = visitor.get(f"/availability/{link}", headers={"Accept":"application/json"})
    assert response.status_code == 200
    return visitor.post(f"/availability/{link}/verify-pin", json={"csrf":response.json()["csrf"],"volunteer_id":person or str(uuid.uuid4()),"pin":pin}, headers={"Origin":ORIGIN})


def counter(stack, scope):
    with closing(sqlite3.connect("file:"+stack.test_database+"?mode=ro",uri=True)) as c:
        result = c.execute("SELECT count FROM auth_throttles WHERE scope_hash=?",(digest(scope),)).fetchone()
        return result[0] if result else 0


@pytest.mark.parametrize("stack", [{**TUNNEL,"environment":{"PIN_IP_LIMIT":"2"}}], indirect=True)
def test_tunnel_distinguishes_visitors_and_ignores_forged_xff_real_ip(stack):
    person, pin, link = fixture_data(stack)
    with client(stack,"203.0.113.10") as a, client(stack,"203.0.113.11") as b:
        for i in range(2):
            a.headers.update({"X-Real-IP":f"198.51.100.{i+1}", "X-Forwarded-For":f"198.51.100.{i+2}, 192.0.2.1", "Forwarded":f"for=198.51.100.{i+3}"})
            assert attempt(a,link).status_code == 401
        a.headers.update({"X-Real-IP":"198.51.100.99", "X-Forwarded-For":"198.51.100.100"})
        assert attempt(a,link,person,pin).status_code == 429
        assert attempt(b,link,person,pin).status_code == 200
    assert counter(stack,"pin-ip:203.0.113.10") == 2
    assert counter(stack,"pin-ip:203.0.113.11") == 1
    assert counter(stack,"pin-ip:198.51.100.99") == 0


@pytest.mark.parametrize("stack", [{**TUNNEL,"environment":{"PIN_ACCOUNT_LIMIT":"2"}}, {**TUNNEL,"environment":{"PIN_GLOBAL_LIMIT":"2"}}], indirect=True)
def test_tunnel_volunteer_and_global_budgets_span_visitors(stack):
    person, pin, link = fixture_data(stack)
    for ip in ("203.0.113.10","203.0.113.11"):
        with client(stack,ip) as visitor:
            assert attempt(visitor,link,person).status_code == 401
    with client(stack,"203.0.113.12") as visitor:
        assert attempt(visitor,link,person,pin).status_code == 429


@pytest.mark.parametrize("stack", [{**TUNNEL,"environment":{"ROSTER_IP_LIMIT":"2","AUTH_IP_LIMIT":"2"}}], indirect=True)
def test_tunnel_roster_and_login_buckets_are_separate_per_visitor(stack):
    person, pin, link = fixture_data(stack)
    with client(stack,"203.0.113.10") as a, client(stack,"203.0.113.11") as b:
        for _ in range(2): assert a.get(f"/availability/{link}").status_code == 200
        assert a.get(f"/availability/{link}").status_code == 429
        assert b.get(f"/availability/{link}").status_code == 200
        def bad_login(visitor, username):
            page = visitor.get("/login")
            csrf = re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
            return visitor.post("/login",json={"csrf":csrf,"username":username,"password":"wrong"},headers={"Origin":ORIGIN})
        for i in range(2): assert bad_login(a,f"missing-{i}").status_code == 401
        assert bad_login(a,"missing-third").status_code == 429
        assert bad_login(b,"missing-fourth").status_code == 401
    assert counter(stack,"ip:203.0.113.10") == 2
    assert counter(stack,"ip:203.0.113.11") == 1


@pytest.mark.parametrize("stack", [{**TUNNEL,"environment":{"PIN_IP_LIMIT":"2"}}], indirect=True)
def test_untrusted_actual_peer_cannot_impersonate_cloudflare(stack):
    person, pin, link = fixture_data(stack)
    with client(stack,"203.0.113.10",peer="127.0.0.2") as outsider:
        for i in range(2):
            outsider.headers.update({"CF-Connecting-IP":f"203.0.113.{i+1}","X-Forwarded-For":f"203.0.113.{i+2}","X-Real-IP":f"203.0.113.{i+3}","Forwarded":"for=127.0.0.1"})
            assert attempt(outsider,link).status_code == 401
        outsider.headers.update({"CF-Connecting-IP":"203.0.113.99","X-Forwarded-For":"203.0.113.100","X-Real-IP":"203.0.113.101"})
        assert attempt(outsider,link,person,pin).status_code == 429
    assert counter(stack,"pin-ip:127.0.0.2") == 2
    assert counter(stack,"pin-ip:203.0.113.99") == 0
    # Missing CF falls back to the TCP peer, never another supplied header.
    with client(stack,"203.0.113.200") as no_cf:
        no_cf.headers.clear()
        no_cf.headers.update({"X-Forwarded-For":"203.0.113.10","X-Real-IP":"203.0.113.10"})
        assert attempt(no_cf,link).status_code == 401
    assert counter(stack,"pin-ip:127.0.0.1") == 1


def test_direct_local_mode_ignores_forged_cf_and_forwarding_headers(stack):
    person, pin, link = fixture_data(stack)
    for i in range(2):
        with client(stack,f"203.0.113.{i+1}") as direct:
            direct.headers.update({"X-Real-IP":f"192.0.2.{i+1}","X-Forwarded-For":f"198.51.100.{i+1}"})
            assert attempt(direct,link).status_code == 401
    assert counter(stack,"pin-ip:127.0.0.1") == 2
    assert counter(stack,"pin-ip:203.0.113.1") == 0
