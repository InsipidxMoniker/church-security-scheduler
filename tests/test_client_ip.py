"""Proxy boundary and persisted throttles, using migrated on-disk SQLite."""
import ipaddress
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from conftest import password_login
from phase2_helpers import setup, verify
from scheduler.app import create_app
from scheduler.client_ip import client_ip
from scheduler.db import row
from scheduler.security import Problem, digest

TRUSTED = [ipaddress.ip_network("172.30.19.2/32")]


def request(peer, headers):
    return Request({"type":"http", "client":(peer, 12000), "headers":[(k.lower().encode(),v.encode()) for k,v in headers]})


@pytest.mark.parametrize("peer", ["198.51.100.9", "::ffff:198.51.100.9"])
def test_untrusted_peer_ignores_all_forged_headers(peer):
    headers = [("CF-Connecting-IP","203.0.113.1"), ("X-Forwarded-For","203.0.113.2"), ("X-Real-IP","203.0.113.3"), ("Forwarded","for=203.0.113.4")]
    assert client_ip(request(peer, headers), TRUSTED) == "198.51.100.9"


@pytest.mark.parametrize("value,expected", [
    ("203.0.113.8","203.0.113.8"), ("2001:0DB8:0000::1","2001:db8::1"),
    ("::ffff:203.0.113.8","203.0.113.8"),
])
def test_trusted_proxy_uses_only_one_canonical_ip(value, expected):
    assert client_ip(request("172.30.19.2", [("X-Real-IP", value), ("CF-Connecting-IP","192.0.2.20"), ("X-Forwarded-For","192.0.2.21")]), TRUSTED) == expected


@pytest.mark.parametrize("values", [[], [""], ["unknown"], ["203.0.113.8, 203.0.113.9"], ["203.0.113.8:4000"], ["fe80::1%eth0"], ["203.0.113.8","203.0.113.9"]])
def test_invalid_or_ambiguous_trusted_normalization_fails_closed(values):
    headers = [("X-Real-IP", v) for v in values] + [("CF-Connecting-IP","192.0.2.1"), ("X-Forwarded-For","192.0.2.2")]
    with pytest.raises(Problem) as failure:
        client_ip(request("172.30.19.2", headers), TRUSTED)
    assert failure.value.status == 400


@contextmanager
def proxy_app(world, monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "172.30.19.2/32")
    app = create_app(world.db, world.security, "https://testserver", lambda: world.now[0])
    with TestClient(app, base_url="https://testserver"):
        yield app


def visitor(app, ip, peer="172.30.19.2"):
    return TestClient(app, base_url="https://testserver", client=(peer, 12000), headers={"X-Real-IP":ip})


def test_two_pin_clients_have_independent_per_ip_budgets(world, monkeypatch):
    person, pin, day, link, _ = setup(world)
    with proxy_app(world, monkeypatch) as app:
        app.state.flow.pin_ip_limit = 2
        a, b = visitor(app,"203.0.113.10"), visitor(app,"203.0.113.11")
        for _ in range(2):
            assert verify(a, link, str(uuid.uuid4()), "wrong").status_code == 401
        a.headers.update({"X-Forwarded-For":"192.0.2.88", "CF-Connecting-IP":"192.0.2.89"})
        assert verify(a, link, person["id"], pin).status_code == 429
        assert verify(b, link, person["id"], pin).status_code == 200
        with world.db.engine.connect() as c:
            for ip, count in (("203.0.113.10",2), ("203.0.113.11",1)):
                assert row(c,"SELECT * FROM auth_throttles WHERE scope_hash=?",(digest("pin-ip:"+ip),))["count"] == count


@pytest.mark.parametrize("scope", ["volunteer", "global"])
def test_cross_ip_volunteer_and_global_limits_remain_shared(world, monkeypatch, scope):
    person, pin, day, link, _ = setup(world)
    with world.db.transaction() as c:
        c.exec_driver_sql("DELETE FROM auth_throttles")  # Exclude fixture's successful PIN verification.
    with proxy_app(world, monkeypatch) as app:
        if scope == "volunteer": app.state.flow.pin_account_limit = 2
        else: app.state.flow.pin_global_limit = 2
        for ip in ("203.0.113.10", "203.0.113.11"):
            assert verify(visitor(app, ip), link, person["id"], "wrong").status_code == 401
        assert verify(visitor(app,"203.0.113.12"), link, person["id"], pin).status_code == 429


def test_public_roster_and_staff_login_use_the_same_boundary(world, monkeypatch):
    person, pin, day, link, _ = setup(world)
    with proxy_app(world, monkeypatch) as app:
        app.state.flow.roster_ip_limit = 2
        app.state.auth.ip_limit = 2
        a, b = visitor(app,"203.0.113.10"), visitor(app,"203.0.113.11")
        for _ in range(2): assert a.get(f"/availability/{link}").status_code == 200
        assert a.get(f"/availability/{link}").status_code == 429
        assert b.get(f"/availability/{link}").status_code == 200
        for i in range(2): assert password_login(a, username=f"missing-{i}", password="wrong").status_code == 401
        assert password_login(a, username="missing-3", password="wrong").status_code == 429
        assert password_login(b, username="missing-4", password="wrong").status_code == 401


def test_direct_app_header_forgery_cannot_rotate_pin_buckets(world, monkeypatch):
    person, pin, day, link, _ = setup(world)
    with proxy_app(world, monkeypatch) as app:
        app.state.flow.pin_ip_limit = 2
        direct = visitor(app,"192.0.2.1", peer="198.51.100.10")
        for i in range(2):
            direct.headers.update({"X-Real-IP":f"192.0.2.{i+1}", "CF-Connecting-IP":f"192.0.2.{i+2}", "X-Forwarded-For":f"192.0.2.{i+3}"})
            assert verify(direct, link, str(uuid.uuid4()), "wrong").status_code == 401
        direct.headers.update({"X-Real-IP":"192.0.2.100", "CF-Connecting-IP":"192.0.2.101", "X-Forwarded-For":"192.0.2.102"})
        assert verify(direct, link, person["id"], pin).status_code == 429
        with world.db.engine.connect() as c:
            assert row(c,"SELECT * FROM auth_throttles WHERE scope_hash=?",(digest("pin-ip:198.51.100.10"),))["count"] == 2


def test_equivalent_ip_spellings_cannot_get_new_pin_budget(world, monkeypatch):
    person, pin, day, link, _ = setup(world)
    with proxy_app(world, monkeypatch) as app:
        app.state.flow.pin_ip_limit = 1
        assert verify(visitor(app,"203.0.113.10"), link, str(uuid.uuid4()), "wrong").status_code == 401
        assert verify(visitor(app,"::ffff:203.0.113.10"), link, person["id"], pin).status_code == 429
