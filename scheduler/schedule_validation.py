"""Domain validation, independent of CP-SAT variables and constraints.

Snapshots contain eligibility only: never credentials, sessions or shared links.
The validator is also used on live draft review, so invalid rows remain visible.
"""
from collections import Counter
from datetime import date
from uuid import UUID

SLOT_KEYS = tuple((s, p) for s in (1, 2) for p in range(1, 9))


def zone(position):
    return "outside" if position in (1, 2, 3, 8) else "inside"


def validate_snapshot(data):
    day = date.fromisoformat(data["local_date"])
    if day.weekday() != 6 or day.isoformat() != data["local_date"]:
        raise ValueError("Invalid Sunday")
    people = data["volunteers"]
    ids = [v["id"] for v in people]
    if len(set(ids)) != len(ids) or any(str(UUID(v)) != v for v in ids):
        raise ValueError("Volunteer UUIDs must be unique and normalized")
    if any(type(v["outside_available"]) is not bool or type(v["rifle_qualified"]) is not bool
           or (v["rifle_qualified"] and not v["outside_available"]) for v in people):
        raise ValueError("Invalid qualification pair")
    positions = data["config"]["positions"]
    if [p["number"] for p in positions] != list(range(1, 9)) or any(
        p["zone"] != zone(p["number"]) or bool(p["restricted_required"]) != (p["number"] in (1, 2))
        for p in positions
    ):
        raise ValueError("Invalid fixed position mapping")
    if [(s["service_number"], s["position_number"]) for s in data["slots"]] != list(SLOT_KEYS):
        raise ValueError("Exactly sixteen ordered input slots required")
    for s in data["slots"]:
        p = positions[s["position_number"] - 1]
        if s["zone"] != p["zone"] or bool(s["essential"]) != bool(p["essential"]):
            raise ValueError("Slot/configuration snapshot mismatch")


def validate_assignments(data, assignments, *, locks_only=False):
    """Return actionable conflicts; manual rotation approvals never waive this check."""
    errors = []
    keys = [(s.get("service_number"), s.get("position_number")) for s in assignments]
    if Counter(keys) != Counter(SLOT_KEYS):
        return ["Schedule must contain exactly the sixteen distinct service/position slots."]
    people = {v["id"]: v for v in data["volunteers"]}
    previous = {(s["service_number"], s["position_number"]): s for s in data["slots"]}
    seen_service, seen_zone = {}, {}
    for slot in assignments:
        service, position, person_id = slot["service_number"], slot["position_number"], slot["volunteer_id"]
        old = previous[service, position]
        label = f"Service {service}, position {position}"
        if old["locked"] and old["volunteer_id"] != person_id:
            errors.append(f"{label}: locked assignment or intentional vacancy changed.")
        if locks_only and not old["locked"]:
            continue
        if person_id is None:
            continue
        person = people.get(person_id)
        if not person or not person["active"]:
            errors.append(f"{label}: volunteer is missing or inactive; repair or unlock before generating.")
        elif person[f"service_{service}_available"] is not True:
            response = "has no response" if person[f"service_{service}_available"] is None else "is unavailable"
            errors.append(f"{label}: volunteer {response}; confirm availability or repair/unlock.")
        elif position in (1, 2) and not person["outside_available"]:
            errors.append(f"{label}: Outside Available qualification required; repair or unlock.")
        if (person_id, service) in seen_service:
            errors.append(f"{label}: duplicate volunteer within service (also position {seen_service[person_id, service]}).")
        if (person_id, zone(position)) in seen_zone:
            errors.append(f"{label}: same-zone pair; strict generation cannot preserve it, even with manual approval. Repair/unlock or finish manually.")
        seen_service[person_id, service] = position
        seen_zone[person_id, zone(position)] = (service, position)
    return errors


def objective_values(data, assignments):
    previous = {(s["service_number"], s["position_number"]): s for s in data["slots"]}
    counts = Counter(s["volunteer_id"] for s in assignments if s["volunteer_id"] is not None)
    rifle_ids = {v["id"] for v in data["volunteers"] if v["rifle_qualified"]}
    rifle_slots = [s for s in assignments if s["position_number"] in (1, 2) and s["volunteer_id"] in rifle_ids]
    result = dict(E=0, O=0, RS=len({s["service_number"] for s in rifle_slots}), RT=len(rifle_slots),
                  RP1=sum(s["position_number"] == 1 for s in rifle_slots),
                  B=sum(n == 2 for n in counts.values()), U=0)
    for s in assignments:
        old = previous[s["service_number"], s["position_number"]]
        if s["volunteer_id"] is not None:
            result["E" if old["essential"] else "O"] += 1
            result["U"] += int(not old["locked"] and old["volunteer_id"] == s["volunteer_id"])
    return result


def diagnostics(data, assignments):
    people = [v for v in data["volunteers"] if v["active"]]
    return {
        "active": len(people),
        "no_response": sum(v["service_1_available"] is None for v in people),
        "neither": sum(v["service_1_available"] is False and v["service_2_available"] is False for v in people),
        "services": [{"service_number": s, "available": sum(v[f"service_{s}_available"] is True for v in people),
                      "outside_available_count": sum(v[f"service_{s}_available"] is True and v["outside_available"] for v in people),
                      "rifle_available": sum(v[f"service_{s}_available"] is True and v["rifle_qualified"] for v in people)} for s in (1, 2)],
        "rifle_objectives": {k: v for k, v in objective_values(data, assignments).items() if k in ("RS", "RT", "RP1")},
        "vacancies": [s for s in assignments if s["volunteer_id"] is None],
        "locks": [s for s in data["slots"] if s["locked"]],
        "explanation": "Vacancies reflect availability, qualifications, locks and strict zone rotation together. A different placement may fill a particular vacancy. Four Positions 1/2 service-slots require four distinct Outside Available people with compatible availability and locks. Coverage is proven maximal only for completed E/O stages. Rifle coverage is advisory, counts only Positions 1/2, and never requires vacancy approval.",
    }
