import copy
import random

import pytest
from ortools.sat.python import cp_model

from scheduler.schedule_validation import validate_assignments
from scheduler.solver import canonical_people, solve
from solver_helpers import exhaustive_oracle, independent_check, independent_score, snapshot


@pytest.mark.parametrize("count,qualified,filled,restricted", [(8, 4, 16, 4), (7, 4, 14, 4), (8, 3, 15, 3), (0, 0, 0, 0), (13, 8, 16, 4)])
def test_coverage_rotation_and_unbounded_qualification(count, qualified, filled, restricted):
    data = snapshot(count, qualified)
    result = solve(data)
    assert result["metadata"]["status"] == "OPTIMAL"
    independent_check(data, result["assignments"])
    assert sum(s["volunteer_id"] is not None for s in result["assignments"]) == filled
    assert sum(s["volunteer_id"] is not None and s["position_number"] < 3 for s in result["assignments"]) == restricted


def test_affirmative_availability_active_and_qualification():
    data = snapshot(6, 2)
    data["volunteers"][0]["service_2_available"] = False
    data["volunteers"][1]["service_2_available"] = None
    data["volunteers"][2]["service_1_available"] = data["volunteers"][2]["service_2_available"] = False
    data["volunteers"][3]["service_1_available"] = data["volunteers"][3]["service_2_available"] = None
    data["volunteers"][4]["active"] = False
    result = solve(data)
    independent_check(data, result["assignments"])
    assert not any(s["volunteer_id"] and s["service_number"] == 2 and s["position_number"] < 3 for s in result["assignments"])
    assert not any(s["volunteer_id"] in {v["id"] for v in data["volunteers"][2:5]} for s in result["assignments"])


@pytest.mark.parametrize("kind", ["inactive", "missing", "unavailable", "unqualified", "same_zone", "duplicate"])
def test_invalid_locks_block_without_approvals_waiving_rules(kind):
    data = snapshot()
    data["slots"][0].update(locked=True, volunteer_id=data["volunteers"][0]["id"])
    if kind == "inactive": data["volunteers"][0]["active"] = False
    if kind == "missing": data["volunteers"].pop(0)
    if kind == "unavailable": data["volunteers"][0]["service_1_available"] = False
    if kind == "unqualified": data["volunteers"][0]["outside_available"] = False
    if kind == "same_zone": data["slots"][8].update(locked=True, volunteer_id=data["volunteers"][0]["id"])
    if kind == "duplicate": data["slots"][1].update(locked=True, volunteer_id=data["volunteers"][0]["id"])
    result = solve(data)
    assert result["metadata"]["status"] == "LOCK_CONFLICT"
    assert result["assignments"] is None and result["problems"]


def test_valid_assignment_and_intentional_vacancy_locks():
    data = snapshot()
    data["slots"][0].update(locked=True, volunteer_id=data["volunteers"][0]["id"])
    data["slots"][1]["locked"] = True
    result = solve(data)
    independent_check(data, result["assignments"])
    assert result["assignments"][1]["volunteer_id"] is None


@pytest.mark.parametrize("seed", range(25))
def test_full_model_generated_inputs(seed):
    randomizer = random.Random(seed)
    data = snapshot(randomizer.randrange(1, 18), randomizer.randrange(0, 10), essential=randomizer.sample(range(1, 9), randomizer.randrange(9)))
    for v in data["volunteers"]:
        v.update(active=randomizer.choice([True, True, False]), service_1_available=randomizer.choice([None, False, True]),
                 service_2_available=randomizer.choice([None, False, True]))
        v["rifle_qualified"] = v["outside_available"] and randomizer.choice([True, False])
    first = solve(data)
    assert first["metadata"]["status"] == "OPTIMAL"
    independent_check(data, first["assignments"])
    for old, assigned in zip(data["slots"], first["assignments"]):
        old.update(volunteer_id=assigned["volunteer_id"], locked=randomizer.choice([False, True]))
    second = solve(data)
    independent_check(data, second["assignments"])
    assert second["metadata"]["status"] == "OPTIMAL"


@pytest.mark.parametrize("seed", range(30))
def test_exhaustive_independent_oracle_all_objectives_and_vector(seed):
    rng = random.Random(seed)
    data = snapshot(3, rng.randrange(4), essential=rng.sample(range(1, 9), rng.randrange(9)))
    free = {(1, 1), (1, 4), (2, 2), (2, 5)}
    for s in data["slots"]:
        s["locked"] = (s["service_number"], s["position_number"]) not in free
        if not s["locked"]:
            s["volunteer_id"] = rng.choice([None] + [v["id"] for v in data["volunteers"]])
    for v in data["volunteers"]:
        v["service_1_available"] = rng.choice([True, True, False, None])
        v["service_2_available"] = rng.choice([True, True, False, None])
        v["rifle_qualified"] = v["outside_available"] and rng.choice([True, False])
    winner, legal = exhaustive_oracle(data)
    actual = solve(data)
    assert actual["metadata"]["status"] == "OPTIMAL"
    assert independent_score(data, actual["assignments"]) == independent_score(data, winner), (seed, legal)
    assert actual["assignments"] == winner


def test_sunday_specific_canonical_vector_repeat_and_rename():
    outputs, rankings = [], []
    for day in ("2026-10-25", "2026-11-01"):
        data = snapshot(3, 3, day=day)
        for s in data["slots"]:
            s["locked"] = (s["service_number"], s["position_number"]) not in {(1, 1), (1, 4), (2, 2), (2, 5)}
        expected, _ = exhaustive_oracle(data)
        actual = solve(data)
        assert actual["assignments"] == expected
        for v in data["volunteers"]: v["display_name"] = "Renamed"
        assert solve(data)["assignments"] == actual["assignments"]
        outputs.append(actual["assignments"])
        rankings.append([v["id"] for v in canonical_people(day, data["volunteers"])])
    assert rankings[0] != rankings[1]
    assert outputs[0] != outputs[1]


def status_factory(statuses):
    """Real incumbents, controlled proof statuses. No fabricated assignments."""
    remaining = iter(statuses)
    class Solver:
        def __init__(self):
            self.real = cp_model.CpSolver()
            self.parameters = self.real.parameters
        def solve(self, model):
            self.real.solve(model)
            return next(remaining)
        def __getattr__(self, name): return getattr(self.real, name)
    return Solver


@pytest.mark.parametrize("statuses,completed,status", [
    ([cp_model.FEASIBLE], [], "FEASIBLE"),
    ([cp_model.OPTIMAL, cp_model.UNKNOWN], ["E"], "FEASIBLE"),
    ([cp_model.UNKNOWN], [], "UNKNOWN/TIMEOUT"),
    ([cp_model.MODEL_INVALID], [], "MODEL_INVALID"),
    ([cp_model.INFEASIBLE], [], "INFEASIBLE"),
    ([cp_model.OPTIMAL]*7 + [cp_model.UNKNOWN], ["E", "O", "RS", "RT", "RP1", "B", "U"], "FEASIBLE"),
])
def test_honest_stage_status_and_incumbent_retention(statuses, completed, status):
    data = snapshot(3, 1)
    result = solve(data, solver_factory=status_factory(statuses))
    assert result["metadata"]["status"] == status
    assert result["metadata"]["completed_stages"] == completed
    assert len(result["metadata"]["stages"]) == len(statuses)
    assert not result["metadata"]["canonicalization_complete"]
    if result["assignments"] is not None:
        independent_check(data, result["assignments"])


def test_total_budget_includes_model_and_stages():
    result = solve(snapshot(), budget=0)
    assert result["assignments"] is None and result["metadata"]["status"] == "UNKNOWN/TIMEOUT"
    # Every stage consumes from one clock, with no resetting between objectives.
    ticks = iter([0, 0, 0.6, 1.2, 1.3])
    result = solve(snapshot(1, 1), budget=1, clock=lambda: next(ticks))
    assert result["metadata"]["completed_stages"] == ["E", "O"]
    assert result["metadata"]["status"] == "FEASIBLE"


def test_validator_rejects_malformed_or_illegal_worker_output():
    data = snapshot()
    assert validate_assignments(data, [])
    bad = copy.deepcopy(data["slots"])
    bad[0]["volunteer_id"] = data["volunteers"][-1]["id"]
    assert any("qualification" in e for e in validate_assignments(data, bad))


def test_essential_coverage_beats_optional_stability_and_both_preference():
    data = snapshot(1, 1, essential={1})
    # Keeping the previous inside assignment would permit one doubled volunteer,
    # but prevents the essential assignment in that same service.
    for s in data["slots"]:
        s["locked"] = (s["service_number"], s["position_number"]) not in {(1, 1), (1, 4), (2, 3)}
    data["slots"][3]["volunteer_id"] = data["volunteers"][0]["id"]
    expected, _ = exhaustive_oracle(data)
    result = solve(data)
    assert result["assignments"] == expected
    assert result["metadata"]["objectives"] == {"E": 1, "O": 0, "RS": 0, "RT": 0, "RP1": 0, "B": 0, "U": 0}


def test_optional_coverage_beats_stability_and_both_is_maximized_at_equal_coverage():
    data = snapshot(3, 0, essential=[])
    data["volunteers"][1]["service_2_available"] = False
    data["volunteers"][2]["service_1_available"] = False
    for s in data["slots"]:
        s["locked"] = (s["service_number"], s["position_number"]) not in {(1, 3), (1, 4), (2, 3), (2, 4)}
    result = solve(data)
    expected, _ = exhaustive_oracle(data)
    assert result["assignments"] == expected
    assert result["metadata"]["objectives"] == {"E": 0, "O": 4, "RS": 0, "RT": 0, "RP1": 0, "B": 1, "U": 0}


def test_model_invalid_snapshot_and_normalized_uuid_hash():
    data = snapshot(1, 1)
    data["config"]["positions"][0]["zone"] = "inside"
    assert solve(data)["metadata"]["status"] == "MODEL_INVALID"
    data = snapshot(1, 1)
    lower = "abcdef01-0000-0000-0000-000000000001"
    a = canonical_people(data["local_date"], [{"id": lower}, {"id": data["volunteers"][0]["id"]}])
    b = canonical_people(data["local_date"], [{"id": lower.upper()}, {"id": data["volunteers"][0]["id"]}])
    assert [v["id"].lower() for v in a] == [v["id"].lower() for v in b]
