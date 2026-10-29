"""Functional five-Sunday growth workload; not the dedicated VM timing benchmark."""
import json
import os
from pathlib import Path
import time
import itertools
import uuid
from types import SimpleNamespace

from sqlalchemy import event

from conftest import post, sign_in
from phase2_helpers import create_person, open_date, settings_payload
from test_generation import generate, review
from test_publication import approve, publish, amend


def test_100_volunteers_five_sundays_publication_amendment_and_bounded_queries(world, monkeypatch, tmp_path):
    sequence = itertools.count()
    # Stable synthetic identities reproduce the Sunday-specific tie-break inputs.
    monkeypatch.setattr("scheduler.volunteer_flow.uuid", SimpleNamespace(uuid4=lambda: uuid.uuid5(uuid.NAMESPACE_URL, f"phase4-growth-v1|{next(sequence)}")))
    sign_in(world)
    assert post(world.client, "/staff/settings", **settings_payload(essential_8=False)).status_code == 200
    people, query_counts, outcomes = [], {}, []
    def count_query(conn, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)
    for i in range(100):
        people.append(create_person(world.client, f"Synthetic growth {i:03}", outside_available=i < 16))
        if i in (19,99):
            day = open_date(world.client)
            queries = []
            event.listen(world.db.engine, "before_cursor_execute", count_query)
            try:
                assert world.client.get(f"/staff/dates/{day['id']}").status_code == 200
                query_counts[str(i+1)] = len(queries)
            finally:
                event.remove(world.db.engine, "before_cursor_execute", count_query)
    assert query_counts["20"] == query_counts["100"] <= 10
    assert review(world, day)["candidates"]["total"] == 100
    for index, sunday in enumerate(("2026-11-01", "2026-11-08", "2026-11-15", "2026-11-22", "2026-11-29")):
        day = open_date(world.client, sunday)
        for i, person in enumerate(people):
            # Distinguish no response from explicit neither, with >4 qualified.
            if i % 5 == 4:
                continue
            assert post(world.client, f"/staff/dates/{day['id']}/availability/{person['id']}", expected_revision=0,
                        service_1_available=i % 5 in (0,1), service_2_available=i % 5 in (0,2), confirmation_reason="Synthetic growth fixture confirmed").status_code == 200
        started = time.monotonic()
        assert generate(world, day).status_code == 200
        data = review(world, day)
        outcomes.append({"date":sunday, "seconds":time.monotonic()-started, "status":data["metadata"]["status"],
                         "completed_stages":data["metadata"]["completed_stages"], "canonicalization_complete":data["metadata"]["canonicalization_complete"]})
        assert not data["problems"] and len(data["slots"]) == 16
        vacancies = [s for s in data["slots"] if s["essential"] and not s["volunteer_id"]]
        if vacancies:
            assert approve(world, day).status_code == 200
        published = publish(world, day).json()["version_id"]
        assert amend(world, day, published).status_code == 200
        if vacancies:
            assert approve(world, day).status_code == 200
        assert publish(world, day, key="amendment").status_code == 200
    with world.db.engine.connect() as c:
        plans = [tuple(r) for r in c.exec_driver_sql("EXPLAIN QUERY PLAN SELECT * FROM availability WHERE date_id=?", (day["id"],))]
        assert any("INDEX" in r[3] for r in plans)
    # The documented test container has a read-only /app. Default to pytest's
    # writable temporary directory; an explicit mounted directory can retain QA.
    results = Path(os.environ.get("TEST_RESULTS_DIR", str(tmp_path)))
    results.mkdir(parents=True, exist_ok=True)
    (results / "growth.json").write_text(json.dumps({"environment":"functional Docker Desktop, not baseline acceptance", "query_counts":query_counts,"plans":plans,"generations":outcomes}, indent=2))
