"""Independent exhaustive oracle for the seven-stage feature contract."""
import copy

import pytest
from ortools.sat.python import cp_model

from scheduler.solver import solve
from solver_helpers import exhaustive_oracle, independent_check, independent_score, snapshot
from test_solver import status_factory


def fixture(free, qualifications, essential=()):
    data = snapshot(len(qualifications), len(qualifications), essential=essential)
    for v, (rifle, first, second) in zip(data["volunteers"], qualifications):
        v.update(rifle_qualified=rifle, service_1_available=first, service_2_available=second)
    for s in data["slots"]:
        s["locked"] = (s["service_number"], s["position_number"]) not in free
    return data


def cases():
    # Essential inside staffing beats an optional rifle assignment.
    yield fixture({(1, 1), (1, 4)}, [(True, True, False)], {4}), (1, 0, 0, 0, 0, 0, 0)
    # Two optional assignments with rotation beat one rifle assignment.
    yield fixture({(1, 1), (1, 4), (2, 3)}, [(True, True, True)]), (0, 2, 0, 0, 0, 1, 0)
    # Two rifles must be distributed across services, even with an incumbent pair
    # concentrated in service 1. RT=2 in both alternatives; RS dominates U.
    data = fixture({(1, 1), (1, 2), (2, 1), (2, 2)}, [(True, True, True)] * 2)
    data["slots"][0]["volunteer_id"] = data["volunteers"][0]["id"]
    data["slots"][1]["volunteer_id"] = data["volunteers"][1]["id"]
    yield data, (0, 2, 2, 2, 2, 0, 1)
    # After distribution, maximize total rifle assignments (one/many rifles).
    for n in (0, 1, 3, 4):
        yield fixture({(1, 1), (1, 2), (2, 1), (2, 2)}, [(True, True, True)] * n), (0, min(n, 4), min(n, 2), min(n, 4), min(n, 2), 0, 0)
    # Rifle coverage beats both-service staffing: the competing two-person
    # doubled schedule fills all four slots but has only one rifle service.
    quals = [(True, True, True), (False, True, True), (True, True, False), (False, False, True)]
    data = fixture({(1, 1), (1, 4), (2, 1), (2, 4)}, quals)
    yield data, (0, 4, 2, 2, 2, 1, 0)
    # Fixed rifles at position 2 make RS=2 in all solutions. Now RT beats B.
    data = fixture({(1, 1), (1, 4), (2, 1), (2, 4)}, quals + [(True, True, False), (True, False, True)])
    data["slots"][1]["volunteer_id"] = data["volunteers"][4]["id"]
    data["slots"][9]["volunteer_id"] = data["volunteers"][5]["id"]
    yield data, (0, 6, 2, 4, 2, 1, 0)
    # Prefer rifle over a legal incumbent, without treating it as eligibility.
    data = fixture({(1, 1)}, [(True, True, False), (False, True, False)])
    data["slots"][0]["volunteer_id"] = data["volunteers"][1]["id"]
    yield data, (0, 1, 1, 1, 1, 0, 0)
    # Valid non-rifle lock and intentional vacancy override all rifle preferences.
    data = fixture({(2, 1)}, [(True, True, False), (False, True, True)])
    data["slots"][0]["volunteer_id"] = data["volunteers"][1]["id"]
    yield data, (0, 1, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("data,expected", list(cases()))
def test_seven_objectives_and_original_canonical_vector(data, expected):
    winner, _ = exhaustive_oracle(data)
    result = solve(data)
    assert result["metadata"]["status"] == "OPTIMAL"
    independent_check(data, result["assignments"])
    assert independent_score(data, winner)[0] == expected
    assert result["assignments"] == winner
    assert tuple(result["metadata"]["objectives"].values()) == expected


@pytest.mark.parametrize("stage", ["RS", "RT", "RP1"])
@pytest.mark.parametrize("status", [cp_model.FEASIBLE, cp_model.UNKNOWN])
def test_unproved_rifle_stage_stops_lower_priorities(stage, status):
    sequence = ["E", "O", "RS", "RT", "RP1", "B", "U"]
    count = sequence.index(stage)
    data = fixture({(1, 1), (2, 1)}, [(True, True, True)] * 2)
    result = solve(data, solver_factory=status_factory([cp_model.OPTIMAL] * count + [status]))
    assert result["metadata"]["status"] == "FEASIBLE"
    assert result["metadata"]["completed_stages"] == sequence[:count]
    assert [s["stage"] for s in result["metadata"]["stages"]] == sequence[:count + 1]
    assert not result["metadata"]["canonicalization_complete"]
    independent_check(data, result["assignments"])


def test_rifle_never_grants_eligibility_or_counts_positions_3_and_8():
    data = fixture({(1, 3), (2, 8)}, [(True, True, True)])
    result = solve(data)
    assert result["metadata"]["objectives"]["RS"] == result["metadata"]["objectives"]["RT"] == 0
    invalid = copy.deepcopy(data)
    invalid["volunteers"][0]["outside_available"] = False
    assert solve(invalid)["metadata"]["status"] == "MODEL_INVALID"
    data["volunteers"][0].update(outside_available=False, rifle_qualified=False)
    assert sum(s["volunteer_id"] is not None for s in solve(data)["assignments"]) == 1


def test_zone_display_names_never_change_rotation_or_canonical_order():
    data = snapshot(8, 4)
    for v in data["volunteers"][:3]:
        v["rifle_qualified"] = True
    before = solve(data)["assignments"]
    data["config"].update(inside_zone_label="Outside", outside_zone_label="Inside")
    after = solve(data)["assignments"]
    independent_check(data, after)
    assert before == after
