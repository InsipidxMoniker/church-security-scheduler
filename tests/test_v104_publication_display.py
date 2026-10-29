"""Finalized outputs hide zone labels without rewriting published payloads."""
import io
from pypdf import PdfReader
from conftest import post
from phase2_helpers import settings_payload
from test_generation import review
from test_publication import prepare, edit, change, approve, publish, amend


def test_final_outputs_history_amendment_and_draft(world):
    day, people = prepare(world, 1)
    assert post(world.client, "/staff/settings", **settings_payload(2, inside_zone_label="INTERIOR UNIQUE", outside_zone_label="EXTERIOR UNIQUE", service_1_time="09:00", service_2_time="11:00", essential_8=False)).status_code == 200
    assert post(world.client, f"/staff/dates/{day['id']}/draft/refresh", **review(world, day)["expected"]).status_code == 200
    slots = review(world, day)["slots"]
    changes = [change(s, volunteer_id=people[0]["id"] if (s["service_number"], s["position_number"]) in ((1, 1), (2, 2)) else None) for s in slots]
    assert edit(world, day, changes, {people[0]["id"]: "Approved pair"}).status_code == 200
    draft = world.client.get(f"/staff/dates/{day['id']}/draft").text
    assert "INTERIOR UNIQUE" in draft and "EXTERIOR UNIQUE" in draft
    assert approve(world, day).status_code == 200
    first = publish(world, day).json()["version_id"]
    with world.db.engine.connect() as c:
        before = tuple(c.exec_driver_sql("SELECT published_payload,payload_hash FROM schedule_versions WHERE id=?", (first,)).one())
    assert amend(world, day, first).status_code == 200
    pair = next(s for s in review(world, day)["slots"] if s["volunteer_id"])
    assert edit(world, day, [change(pair)], {people[0]["id"]: "Approved again"}).status_code == 200
    assert approve(world, day).status_code == 200
    second = publish(world, day, key="second").json()["version_id"]
    for version in (first, second):
        path = f"/staff/dates/{day['id']}/publications/{version}"
        html = world.client.get(path).text
        text = world.client.get(path + "/signal.txt").text
        pdf = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(world.client.get(path + "/schedule.pdf").content)).pages)
        for output in (html, text, pdf):
            assert "INTERIOR UNIQUE" not in output and "EXTERIOR UNIQUE" not in output
            assert day["local_date"] in output and "First service" in output and "Second service" in output
            assert people[0]["public_label"] in output and "VACANT" in output
            assert "approved" in output.lower() and "optional" in output.lower() and "same-zone" in output.lower()
        assert "1. " + people[0]["public_label"] in text
        assert "09:00" in text and "11:00" in text
    with world.db.engine.connect() as c:
        assert tuple(c.exec_driver_sql("SELECT published_payload,payload_hash FROM schedule_versions WHERE id=?", (first,)).one()) == before
