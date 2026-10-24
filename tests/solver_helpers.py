"""Independent test fixtures/oracle. Does not import production validator/ranking."""
import hashlib
import itertools
from collections import Counter
from uuid import UUID


def snapshot(count=8, qualified=4, day="2026-10-25", essential=None):
    return {"local_date": day,
            "config": {"timezone": "America/Los_Angeles", "service_1_label": "First", "service_2_label": "Second",
                       "service_1_time": None, "service_2_time": None,
                       "positions": [{"number": p, "zone": "outside" if p in (1, 2, 3, 8) else "inside",
                                      "restricted_required": p < 3, "essential": p in (essential if essential is not None else range(1, 9))} for p in range(1, 9)]},
            "volunteers": [{"id": str(UUID(int=i+1)), "active": True, "restricted_qualified": i < qualified,
                            "service_1_available": True, "service_2_available": True} for i in range(count)],
            "slots": [{"service_number": s, "position_number": p, "volunteer_id": None, "locked": False,
                       "slot_revision": 1, "zone": "outside" if p in (1, 2, 3, 8) else "inside",
                       "essential": p in (essential if essential is not None else range(1, 9))} for s in (1, 2) for p in range(1, 9)]}


def independent_check(data, slots):
    assert len(slots) == 16
    assert {(s["service_number"], s["position_number"]) for s in slots} == {(s, p) for s in (1, 2) for p in range(1, 9)}
    people = {v["id"]: v for v in data["volunteers"]}
    services, zones = set(), set()
    original = {(s["service_number"], s["position_number"]): s for s in data["slots"]}
    for s in slots:
        service, position, vid = s["service_number"], s["position_number"], s["volunteer_id"]
        if original[service, position]["locked"]:
            assert vid == original[service, position]["volunteer_id"]
        if vid is None:
            continue
        v = people[vid]
        assert v["active"] and v[f"service_{service}_available"] is True
        assert position > 2 or v["restricted_qualified"]
        assert (service, vid) not in services
        zone = position in (1, 2, 3, 8)
        assert (zone, vid) not in zones
        services.add((service, vid)); zones.add((zone, vid))


def independent_score(data, slots):
    mapping = {(s["service_number"], s["position_number"]): s["volunteer_id"] for s in slots}
    filled = [(s, mapping[s["service_number"], s["position_number"]]) for s in data["slots"]]
    e = sum(vid is not None and s["essential"] for s, vid in filled)
    o = sum(vid is not None and not s["essential"] for s, vid in filled)
    b = sum(n == 2 for n in Counter(vid for _, vid in filled if vid).values())
    u = sum(vid is not None and vid == s["volunteer_id"] and not s["locked"] for s, vid in filled)
    ids = sorted((v["id"] for v in data["volunteers"]), key=lambda vid: (
        hashlib.sha256(("church-scheduler-date-tiebreak-v1|" + data["local_date"] + "|" + str(UUID(vid))).encode()).digest(), UUID(vid).bytes))
    rank = {vid: i for i, vid in enumerate(ids)}
    vector = tuple(rank.get(mapping[s, p], len(ids)) for s in (1, 2) for p in range(1, 9))
    return (e, o, b, u), vector


def exhaustive_oracle(data):
    """Enumerate the Cartesian product, discard illegal full assignments, score independently."""
    choices = []
    for s in data["slots"]:
        if s["locked"]:
            choices.append([s["volunteer_id"]])
        else:
            choices.append([None] + [v["id"] for v in data["volunteers"] if v["active"] and
                                   v[f"service_{s['service_number']}_available"] is True and
                                   (s["position_number"] > 2 or v["restricted_qualified"])])
    best, winner, legal = None, None, 0
    for values in itertools.product(*choices):
        slots = [{"service_number": s["service_number"], "position_number": s["position_number"], "volunteer_id": vid}
                 for s, vid in zip(data["slots"], values)]
        try:
            independent_check(data, slots)
        except AssertionError:
            continue
        legal += 1
        objectives, vector = independent_score(data, slots)
        key = objectives + tuple(-r for r in vector)
        if best is None or key > best:
            best, winner = key, slots
    assert legal > 0
    return winner, legal
