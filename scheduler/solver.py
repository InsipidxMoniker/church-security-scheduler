"""Strict sequential CP-SAT optimization. Imported only by the isolated worker/tests."""
import hashlib
import time
from uuid import UUID

import ortools
from ortools.sat.python import cp_model

from scheduler.schedule_validation import (SLOT_KEYS, objective_values, validate_assignments,
                                           validate_snapshot, zone)

TIEBREAK = "church-scheduler-date-tiebreak-v1"
PARAMETERS = {"num_search_workers": 1, "random_seed": 0, "log_search_progress": False}


def canonical_people(local_date, people):
    return sorted(people, key=lambda v: (hashlib.sha256(
        f"{TIEBREAK}|{local_date}|{str(UUID(v['id']))}".encode("utf-8")).digest(), UUID(v["id"]).bytes))


def solve(data, budget=10.0, *, solver_factory=cp_model.CpSolver, clock=time.monotonic):
    start = clock()
    deadline = start + budget
    metadata = {"status": "UNKNOWN/TIMEOUT", "stages": [], "completed_stages": [],
                "last_completed_stage": None, "canonicalization_complete": False, "canonical_slots": 0,
                "objectives": None, "solver_version": ortools.__version__, "parameters": PARAMETERS,
                "budget_seconds": budget, "tie_break_algorithm": TIEBREAK}
    incumbent = None

    def finish(status=None, problems=None):
        metadata["status"] = status or ("FEASIBLE" if incumbent is not None else "UNKNOWN/TIMEOUT")
        metadata["elapsed_seconds"] = clock() - start
        metadata["objectives"] = objective_values(data, incumbent) if incumbent is not None else None
        return {"assignments": incumbent, "metadata": metadata, "problems": problems or []}

    try:
        validate_snapshot(data)
    except (ValueError, KeyError, TypeError):
        return finish("MODEL_INVALID", ["Invalid input snapshot; review date, UUIDs and position snapshots."])
    conflicts = validate_assignments(data, data["slots"], locks_only=True)
    if conflicts:
        return finish("LOCK_CONFLICT", conflicts)
    people = canonical_people(data["local_date"], data["volunteers"])
    model = cp_model.CpModel()
    x, empty, by_slot = {}, {}, {}
    for s, p in SLOT_KEYS:
        candidates = {}
        for v in people:
            if v["active"] and v[f"service_{s}_available"] is True and (p not in (1, 2) or v["outside_available"]):
                x[v["id"], s, p] = candidates[v["id"]] = model.new_bool_var(f"x_{v['id']}_{s}_{p}")
        by_slot[s, p] = candidates
        empty[s, p] = model.new_bool_var(f"vacant_{s}_{p}")
        model.add(sum(candidates.values()) + empty[s, p] == 1)
    both = []
    for v in people:
        vid = v["id"]
        assigned = []
        for s in (1, 2):
            a = model.new_bool_var(f"a_{vid}_{s}")
            model.add(a == sum(x.get((vid, s, p), 0) for p in range(1, 9)))
            assigned.append(a)
        for z in ("inside", "outside"):
            model.add(sum(x.get((vid, s, p), 0) for s, p in SLOT_KEYS if zone(p) == z) <= 1)
        b = model.new_bool_var(f"b_{vid}")
        model.add(b <= assigned[0]); model.add(b <= assigned[1])
        model.add(b >= assigned[0] + assigned[1] - 1)
        both.append(b)
    for slot in data["slots"]:
        s, p, vid = slot["service_number"], slot["position_number"], slot["volunteer_id"]
        if slot["locked"]:
            model.add((empty[s, p] if vid is None else x[vid, s, p]) == 1)
    rifle_counts, rifle_covered = [], []
    for s in (1, 2):
        r = sum(x.get((v["id"], s, p), 0) for v in people if v["rifle_qualified"] for p in (1, 2))
        covered = model.new_bool_var(f"rifle_covered_{s}")
        model.add(covered <= r)
        model.add(r <= 2 * covered)
        rifle_counts.append(r)
        rifle_covered.append(covered)
    objectives = {
        "E": sum(1 - empty[s["service_number"], s["position_number"]] for s in data["slots"] if s["essential"]),
        "O": sum(1 - empty[s["service_number"], s["position_number"]] for s in data["slots"] if not s["essential"]),
        "RS": sum(rifle_covered),
        "RT": sum(rifle_counts),
        "RP1": sum(x.get((v["id"], s, 1), 0) for v in people if v["rifle_qualified"] for s in (1, 2)),
        "B": sum(both),
        "U": sum(x.get((s["volunteer_id"], s["service_number"], s["position_number"]), 0)
                 for s in data["slots"] if not s["locked"] and s["volunteer_id"] is not None),
    }

    def run(stage):
        remaining = deadline - clock()
        if remaining <= 0:
            metadata["stages"].append({"stage": stage, "status": "UNKNOWN", "reason": "total deadline"})
            return cp_model.UNKNOWN, None
        solver = solver_factory()
        for name, value in PARAMETERS.items():
            setattr(solver.parameters, name, value)
        solver.parameters.max_time_in_seconds = remaining
        status = solver.solve(model)
        record = {"stage": stage, "status": solver.status_name(status), "wall_seconds": solver.wall_time,
                  "deterministic_time": solver.response_proto.deterministic_time}
        if stage in objectives:
            record["bound"] = solver.best_objective_bound
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                record["value"] = solver.value(objectives[stage])
        metadata["stages"].append(record)
        return status, solver

    def cache(solver):
        nonlocal incumbent
        candidate = [{"service_number": s, "position_number": p,
                      "volunteer_id": next((vid for vid, var in by_slot[s, p].items() if solver.value(var)), None)}
                     for s, p in SLOT_KEYS]
        if validate_assignments(data, candidate):
            raise ValueError("Invalid solver result")
        incumbent = candidate

    for stage, objective in objectives.items():
        model.maximize(objective)
        status, solver = run(stage)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            cache(solver)
        if status in (cp_model.MODEL_INVALID, cp_model.INFEASIBLE):
            incumbent = None
            return finish(solver.status_name(status))
        if status != cp_model.OPTIMAL:
            return finish()
        model.add(objective == solver.value(objective))
        metadata["completed_stages"].append(stage)
        metadata["last_completed_stage"] = stage

    model.clear_objective()
    for s, p in SLOT_KEYS:
        # Candidate order, then null. Each earlier rejection is proven infeasible;
        # a feasible witness suffices to fix this vector component.
        for vid, literal in [*by_slot[s, p].items(), (None, empty[s, p])]:
            model.clear_assumptions()
            model.add_assumption(literal)
            status, solver = run(f"canonical_{s}_{p}")
            model.clear_assumptions()
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                cache(solver)
                model.add(literal == 1)
                metadata["canonical_slots"] += 1
                break
            if status == cp_model.MODEL_INVALID:
                incumbent = None
                return finish("MODEL_INVALID")
            if status == cp_model.UNKNOWN:
                return finish()
        else:
            incumbent = None
            return finish("INFEASIBLE")
    metadata["canonicalization_complete"] = True
    metadata["last_completed_stage"] = "canonicalization"
    return finish("OPTIMAL")
