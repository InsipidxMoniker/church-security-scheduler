"""Reproducible local timing evidence; dedicated Debian acceptance stays separate."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient

from conftest import post, sign_in
from phase2_helpers import create_person, detail, new_link, open_date, save, settings_payload, verify
from scheduler.generation import SolverProcess
from test_generation import generate, review
from test_publication import amend, approve, change, edit, publish


def p95(values):
    return sorted(values)[math.ceil(len(values) * .95)-1]


def test_100_people_20_solves_five_sundays_and_concurrent_requests(world, monkeypatch, tmp_path):
    ids = iter(uuid.uuid5(uuid.NAMESPACE_URL, f"v103-benchmark|{i}") for i in range(200))
    monkeypatch.setattr("scheduler.volunteer_flow.uuid", SimpleNamespace(uuid4=lambda: next(ids)))
    sign_in(world)
    assert post(world.client, "/staff/settings", **settings_payload(essential_8=False)).status_code == 200
    people = [create_person(world.client, f"Synthetic scale {i:03}", outside_available=i < 16, rifle_qualified=i < 8) for i in range(100)]
    outcomes, ordinary, pin_times, statuses = [], [], [], Counter()
    with ExitStack() as cleanup:
        other = cleanup.enter_context(TestClient(world.app, base_url="https://testserver"))
        sign_in(world, "synthetic-scheduler", other)
        for index, sunday in enumerate(("2026-11-01", "2026-11-08", "2026-11-15", "2026-11-22", "2026-11-29")):
            day = open_date(world.client, sunday)
            for i, person in enumerate(people):
                if i % 5 == 4:
                    continue  # no response is distinct from neither
                first, second = i % 5 in (0, 1), i % 5 in (0, 2)
                if index == 1 and 2 <= i < 16:
                    first = second = False  # qualification shortage
                if index == 3 and i >= 6:
                    first = second = False  # partial staffing
                assert post(world.client, f"/staff/dates/{day['id']}/availability/{person['id']}", expected_revision=0,
                            service_1_available=first, service_2_available=second, confirmation_reason="Synthetic benchmark confirmed").status_code == 200
            if index == 0:
                day = detail(world.client, f"/staff/dates/{day['id']}")["day"]
                link = new_link(world.client, day)["token"]
                volunteers = []
                for i, person in enumerate(people[:20]):
                    pin = post(world.client, f"/staff/volunteers/{person['id']}/pin", expected_revision=1).json()["pin"]
                    client = cleanup.enter_context(TestClient(world.app, base_url="https://testserver"))
                    started = time.monotonic()
                    assert verify(client, link, person["id"], pin).status_code == 200
                    pin_times.append(time.monotonic()-started)
                    volunteers.append(client)
                # Real worker runs in isolation while twenty volunteer sessions
                # and two staff sessions perform 100 ordinary reads/writes. All
                # changed responses must survive; stale solve must not commit.
                entered = threading.Event()
                class ObservedWorker(SolverProcess):
                    async def run(self, snapshot):
                        entered.set()
                        return await super().run(snapshot)
                world.app.state.generation.runner = ObservedWorker()
                def ordinary_client(i):
                    client = volunteers[i]
                    for n in range(4):
                        start = time.monotonic()
                        response = (save(client, link, revision=int(i % 5 != 4), first=True, second=True)
                                    if n == 0 else client.get(f"/availability/{link}/mine"))
                        ordinary.append(time.monotonic()-start)
                        statuses[response.status_code] += 1
                        assert response.status_code == 200, response.text
                def staff_client(client):
                    for n in range(10):
                        start = time.monotonic()
                        response = client.get(f"/staff/dates/{day['id']}")
                        ordinary.append(time.monotonic()-start)
                        statuses[response.status_code] += 1
                        assert response.status_code == 200
                with ThreadPoolExecutor(max_workers=23) as pool:
                    pending = pool.submit(generate, world, day)
                    assert entered.wait(5)
                    jobs = [pool.submit(ordinary_client, i) for i in range(20)] + [pool.submit(staff_client, c) for c in (world.client, other)]
                    for job in jobs:
                        job.result(timeout=15)
                    assert pending.result(timeout=15).status_code == 409
                for i, person in enumerate(people[:20]):
                    mine = volunteers[i].get(f"/availability/{link}/mine", headers={"Accept": "application/json"}).json()["response"]
                    assert mine["revision"] == int(i % 5 != 4) + 1 and mine["service_1_available"] and mine["service_2_available"]
            world.app.state.generation.runner = SolverProcess()
            for run in range(4):
                start = time.monotonic()
                response = generate(world, day)
                assert response.status_code == 200, response.text
                elapsed = time.monotonic()-start
                data = review(world, day)
                metadata = data["metadata"]
                outcomes.append({"date": sunday, "scenario": ["full", "qualification shortage", "locks", "partial", "mixed"][index],
                                 "run": run, "request_seconds": elapsed, **{k: metadata[k] for k in ("status", "completed_stages", "canonicalization_complete", "elapsed_seconds", "worker_elapsed_seconds", "objectives", "stages")}})
                assert not data["problems"] and metadata["budget_seconds"] == 10
                assert metadata["elapsed_seconds"] <= 10.1
                assert metadata["worker_elapsed_seconds"] <= 13
                if index == 2 and run == 0:
                    # Preserve one assignee and one intentionally vacant slot.
                    assigned = next(s for s in data["slots"] if s["volunteer_id"])
                    vacant = next(s for s in data["slots"] if s != assigned)
                    assert edit(world, day, [change(assigned, locked=True), change(vacant, volunteer_id=None, locked=True)]).status_code == 200
            if any(s["essential"] and not s["volunteer_id"] for s in data["slots"]):
                assert approve(world, day).status_code == 200
            version = publish(world, day, key=sunday).json()["version_id"]
            assert amend(world, day, version).status_code == 200
            if any(s["essential"] and not s["volunteer_id"] for s in data["slots"]):
                assert approve(world, day).status_code == 200
            assert publish(world, day, key=sunday+"-amended").status_code == 200
    summary = {"environment": "Docker Desktop Linux container, FastAPI TestClient + real isolated solver; no TLS/proxy in this timing sample; not dedicated Debian acceptance",
               "generation_count": len(outcomes), "ordinary_count": len(ordinary), "http_status_counts": dict(statuses),
               "ordinary_p95_seconds": p95(ordinary), "pin_p95_seconds": p95(pin_times),
               "request_max_seconds": max(o["request_seconds"] for o in outcomes),
               "seven_stage_proofs": sum(o["completed_stages"] == ["E", "O", "RS", "RT", "RP1", "B", "U"] for o in outcomes),
               "canonical_completions": sum(o["canonicalization_complete"] for o in outcomes), "generations": outcomes}
    for name in ("memory.peak", "memory.max", "cpu.max"):
        path = Path("/sys/fs/cgroup") / name
        if path.exists():
            summary[name] = path.read_text().strip()
    output = Path(os.environ.get("TEST_RESULTS_DIR", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    (output / "v103-refinements-benchmark.json").write_text(json.dumps(summary, indent=2))
    assert len(outcomes) == 20 and len(ordinary) == 100
