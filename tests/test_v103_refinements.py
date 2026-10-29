"""Additional work in the same uncommitted v1.0.3 changeset."""
import re
import copy
import json
import pytest

from conftest import PASSWORD
from test_generation import Immediate, generate, initial, review
from scheduler.app import PENDING, STAFF
from scheduler.solver import solve
from solver_helpers import exhaustive_oracle, independent_check
from test_v103_solver import fixture


def test_position_one_rifle_preference_adds_ordered_proof():
    data = fixture({(1, 1), (1, 2)}, [(True, True, False), (False, True, False)])
    result = solve(data)
    assert result["metadata"]["completed_stages"] == ["E", "O", "RS", "RT", "RP1", "B", "U"]
    assert result["assignments"][0]["volunteer_id"] == data["volunteers"][0]["id"]
    assert result["assignments"][1]["volunteer_id"] == data["volunteers"][1]["id"]
    assert result["metadata"]["objectives"]["RP1"] == 1


def test_bad_browser_password_renders_login_and_can_retry(world):
    page = world.client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    response = world.client.post("/login", data={"csrf": csrf, "username": "synthetic-admin", "password": "wrong secret"},
                                 headers={"Accept": "text/html", "Origin": "https://testserver"})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("text/html")
    assert 'role="alert"' in response.text and "Invalid username or password" in response.text
    assert "wrong secret" not in response.text
    assert PENDING not in world.client.cookies and STAFF not in world.client.cookies
    csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
    result = world.client.post("/login", data={"csrf": csrf, "username": "synthetic-admin", "password": PASSWORD},
                              headers={"Accept": "text/html", "Origin": "https://testserver"}, follow_redirects=False)
    assert result.status_code == 303 and result.headers["location"] == "/mfa"


@pytest.mark.parametrize("service", [1, 2])
@pytest.mark.parametrize("rifles", [0, 1, 2])
def test_rp1_standard_one_and_two_rifles_both_services(service, rifles):
    data = fixture({(service, 1), (service, 2)}, [(i < rifles, service == 1, service == 2) for i in range(2)])
    result = solve(data)
    winner, _ = exhaustive_oracle(data)
    assert result["assignments"] == winner
    assert result["metadata"]["status"] == "OPTIMAL"
    pair = [s for s in result["assignments"] if s["service_number"] == service and s["position_number"] in (1, 2)]
    assert all(s["volunteer_id"] for s in pair)
    assert result["metadata"]["objectives"]["RP1"] == int(rifles > 0)
    if rifles == 1:
        assert pair[0]["volunteer_id"] == data["volunteers"][0]["id"]
        assert pair[1]["volunteer_id"] == data["volunteers"][1]["id"]


@pytest.mark.parametrize("service", [1, 2])
@pytest.mark.parametrize("vacancy", [False, True])
def test_rp1_respects_position_one_locks_and_falls_back_to_two(service, vacancy):
    data = fixture({(service, 2)}, [(True, service == 1, service == 2), (False, service == 1, service == 2)])
    one = next(s for s in data["slots"] if s["service_number"] == service and s["position_number"] == 1)
    one["volunteer_id"] = None if vacancy else data["volunteers"][1]["id"]
    result = solve(data)
    independent_check(data, result["assignments"])
    assert result["metadata"]["objectives"]["RP1"] == 0
    assert result["metadata"]["objectives"]["RT"] == 1
    assert next(s for s in result["assignments"] if s["service_number"] == service and s["position_number"] == 2)["volunteer_id"] == data["volunteers"][0]["id"]


def test_rp1_cannot_reduce_essential_coverage_or_override_rotation():
    data = fixture({(1, 1), (1, 2)}, [(True, True, False)], essential={2})
    result = solve(data)
    assert result["metadata"]["objectives"] == dict(E=1, O=0, RS=1, RT=1, RP1=0, B=0, U=0)
    assert result["assignments"][1]["volunteer_id"] == data["volunteers"][0]["id"]
    data = fixture({(1, 1), (2, 1)}, [(True, True, True)])
    result = solve(data)
    independent_check(data, result["assignments"])
    assert result["metadata"]["objectives"]["RS"] == result["metadata"]["objectives"]["RP1"] == 1
    for index in (0, 8):
        data["slots"][index].update(volunteer_id=data["volunteers"][0]["id"], locked=True)
    assert solve(data)["metadata"]["status"] == "LOCK_CONFLICT"


def test_rp1_dominates_unlocked_retention_with_unchanged_coverage():
    data = fixture({(1, 1), (1, 2)}, [(True, True, False), (False, True, False)])
    data["slots"][0]["volunteer_id"] = data["volunteers"][1]["id"]
    data["slots"][1]["volunteer_id"] = data["volunteers"][0]["id"]
    result = solve(data)
    assert result["metadata"]["objectives"] == dict(E=0, O=2, RS=1, RT=1, RP1=1, B=0, U=0)
    assert result["assignments"] == exhaustive_oracle(data)[0]


def test_rp1_never_trades_rifle_distribution_or_total_for_position_one():
    # A standard lock at service 2 position 1 cannot force both rifles into
    # service 1: RS still requires service 2's rifle to go to position 2.
    data = fixture({(1, 1), (1, 2), (2, 2)}, [(True, True, True), (True, True, True), (False, False, True)])
    data["slots"][8]["volunteer_id"] = data["volunteers"][2]["id"]
    result = solve(data)
    assert result["assignments"] == exhaustive_oracle(data)[0]
    assert result["metadata"]["objectives"] == dict(E=0, O=3, RS=2, RT=2, RP1=1, B=0, U=0)


def test_deadline_at_rp1_keeps_previous_incumbent_without_lower_stages():
    data = fixture({(1, 1), (1, 2)}, [(True, True, False), (False, True, False)])
    ticks = iter([0, 0, 0, 0, 0, 1.1, 1.2])
    result = solve(data, budget=1, clock=lambda: next(ticks))
    assert result["metadata"]["completed_stages"] == ["E", "O", "RS", "RT"]
    assert result["metadata"]["stages"][-1] == {"stage": "RP1", "status": "UNKNOWN", "reason": "total deadline"}
    assert result["metadata"]["status"] == "FEASIBLE"
    independent_check(data, result["assignments"])


def test_rp1_preserves_date_canonicalization_and_repeatability():
    data = fixture({(1, 1), (1, 2), (2, 1), (2, 2)}, [(True, True, True)] * 3 + [(False, True, True)])
    for day in ("2026-10-25", "2026-11-01"):
        data["local_date"] = day
        winner, _ = exhaustive_oracle(data)
        assert solve(data)["assignments"] == winner
        renamed = copy.deepcopy(data)
        for v in renamed["volunteers"]:
            v["display_name"] = "Renamed"
        assert solve(renamed)["assignments"] == winner


def test_earlier_six_stage_result_is_historical_without_changing_draft(world):
    day, _ = initial(world, 2)
    world.app.state.generation.runner = Immediate()
    assert generate(world, day).status_code == 200
    with world.db.transaction() as c:
        saved = c.exec_driver_sql("SELECT solver_metadata FROM schedule_versions WHERE status='draft'").scalar()
        metadata = json.loads(saved)
        metadata["objectives"].pop("RP1")
        metadata["completed_stages"].remove("RP1")
        metadata["stages"] = [s for s in metadata["stages"] if s["stage"] != "RP1"]
        historical = json.dumps(metadata)
        c.exec_driver_sql("UPDATE schedule_versions SET solver_metadata=? WHERE status='draft'", (historical,))
    before = review(world, day)
    assert not before["optimization_current"] and not before["problems"]
    page = world.client.get(f"/staff/dates/{day['id']}/draft").text
    assert "Previous solver results describe the earlier snapshot" in page
    assert "All E/O/RS/RT/RP1/B/U objectives proven optimal" not in page
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT solver_metadata FROM schedule_versions WHERE status='draft'").scalar() == historical
    assert generate(world, day).status_code == 200
    assert review(world, day)["optimization_current"]
