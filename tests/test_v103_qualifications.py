"""Feature amendment regressions on the real migrated SQLite database."""
import pytest
from sqlalchemy.exc import IntegrityError
from concurrent.futures import ThreadPoolExecutor
import io
from pypdf import PdfReader

from conftest import post, sign_in
from phase2_helpers import create_person, detail, settings_payload, setup
from solver_helpers import snapshot
from scheduler.solver import solve
from test_generation import Controlled, Immediate, generate, initial, review
from test_publication import approve, publish, edit, change


def test_migrated_database_qualification_subset(world):
    sign_in(world)
    person = create_person(world.client)
    with pytest.raises(IntegrityError), world.db.transaction() as c:
        c.exec_driver_sql("UPDATE volunteers SET outside_available=0,rifle_qualified=1 WHERE id=?", (person["id"],))


def test_rifle_objectives_precede_both_and_retention():
    data = snapshot(4, 4)
    for v in data["volunteers"]:
        v["rifle_qualified"] = True
    result = solve(data)
    assert result["metadata"]["completed_stages"] == ["E", "O", "RS", "RT", "RP1", "B", "U"]
    assert result["metadata"]["objectives"]["RS"] == 2
    assert result["metadata"]["objectives"]["RT"] == 4


@pytest.mark.parametrize("role", ["synthetic-admin", "synthetic-scheduler"])
def test_staff_qualification_defaults_subset_removal_and_audit(world, role):
    sign_in(world, role)
    person = post(world.client, "/staff/volunteers", display_name="Qualified", active=True).json()
    assert person["outside_available"] == person["rifle_qualified"] == 0
    url = f"/staff/volunteers/{person['id']}"
    def save(**values):
        current = detail(world.client, url)["person"]
        return post(world.client, url, **{k: current[k] for k in ("display_name", "public_label", "active")},
                    expected_revision=current["revision"], **values)
    assert save(outside_available=False, rifle_qualified=True).status_code == 422
    p = save(outside_available=True, rifle_qualified=True).json()
    assert p["rifle_qualified"] == p["outside_available"] == 1
    # Omission cannot silently clear a rifle qualification; even explicit false
    # must first be saved separately before removing its parent qualification.
    assert save(outside_available=False).status_code == 422
    assert save(outside_available=False, rifle_qualified=False).status_code == 422
    assert save(outside_available=True, rifle_qualified=False).status_code == 200
    assert save(outside_available=False, rifle_qualified=False).status_code == 200
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar() == 5
        events = c.exec_driver_sql("SELECT details FROM audit_events WHERE object_id=? AND action='roster.updated'", (person["id"],)).scalars().all()
        assert len(events) == 3 and '"rifle_qualified": 1' in events[0]


@pytest.mark.parametrize("labels", [("", "Y"), (None, "Y"), (" ", "Y"), ("Same", " same "), ("Straße", "STRASSE"), ("x"*101, "Y"), ("A\nB", "Y"), ("A\x85B", "Y"), ("A\tB", "Y")])
def test_zone_label_validation_atomic(world, labels):
    sign_in(world)
    assert post(world.client, "/staff/settings", **settings_payload(inside_zone_label=labels[0], outside_zone_label=labels[1])).status_code == 422
    s = detail(world.client, "/staff/settings")
    assert s["settings_revision"] == s["config_epoch"] == 1
    assert (s["inside_zone_label"], s["outside_zone_label"]) == ("Inside", "Outside")


def test_zone_admin_only_refresh_snapshots_historical_exports(world):
    day, _ = initial(world, 8)
    world.app.state.generation.runner = Immediate()
    assert generate(world, day).status_code == 200
    assert publish(world, day).status_code == 200
    with world.db.engine.connect() as c:
        published = dict(c.exec_driver_sql("SELECT * FROM schedule_versions WHERE status='published'").mappings().one())
    root = f"/staff/dates/{day['id']}/publications/{published['id']}"
    old_text = world.client.get(root + "/signal.txt").text
    old_pdf = world.client.get(root + "/schedule.pdf").content
    from test_publication import amend
    assert amend(world, day, published["id"]).status_code == 200
    before = review(world, day)
    sign_in(world, "synthetic-scheduler")
    assert post(world.client, "/staff/settings", **settings_payload(2, inside_zone_label="Hall", outside_zone_label="Grounds")).status_code == 403
    world.now[0] += 30
    sign_in(world)
    saved = post(world.client, "/staff/settings", **settings_payload(2, timezone="Pacific/Auckland", inside_zone_label="  <Hall>  ", outside_zone_label="Grounds")).json()
    assert saved["settings_revision"] == saved["config_epoch"] == 3
    data = review(world, day)
    assert data["stale_configuration"] and data["config"] == before["config"]
    assert data["current_config"]["inside_zone_label"] == "<Hall>"
    assert generate(world, day).status_code == publish(world, day, key="stale").status_code == 409
    assert post(world.client, f"/staff/dates/{day['id']}/draft/refresh", **data["expected"]).status_code == 200
    refreshed = review(world, day)
    assert refreshed["config"]["inside_zone_label"] == "<Hall>"
    assert refreshed["config"]["timezone"] == day["timezone_snapshot"]
    assert [(s["zone"], s["volunteer_id"], s["slot_revision"]) for s in refreshed["slots"]] == [(s["zone"], s["volunteer_id"], s["slot_revision"]) for s in before["slots"]]
    assert "&lt;Hall&gt;" in world.client.get(f"/staff/dates/{day['id']}/draft").text
    assert generate(world, day).status_code == 200
    assert world.client.get(root + "/signal.txt").text == old_text
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(old_pdf)).pages)
    assert "Inside" not in text and "Outside" not in text
    with world.db.engine.connect() as c:
        after = dict(c.exec_driver_sql("SELECT * FROM schedule_versions WHERE status='published'").mappings().one())
        assert after == published


@pytest.mark.parametrize("csv_text,valid,legacy", [
    ("display_name,outside_available,rifle_qualified\nRifle,true,true\nOther,false,false", True, False),
    ("display_name,restricted_qualified\nLegacy,true", True, True),
    ("display_name,outside_available,rifle_qualified\nGood,true,false\nBad,false,true", False, False),
    ("display_name,outside_available,rifle_qualified\nBad,true,TRUE", False, False),
    ("display_name,outside_available,rifle_qualified\nSame,true,false\n same ,true,true", False, False),
])
def test_new_legacy_csv_and_no_partial_import(world, csv_text, valid, legacy):
    sign_in(world)
    preview = post(world.client, "/staff/volunteers/import/preview", csv_text=csv_text).json()
    assert bool(preview["preview"]) == valid
    if not valid:
        assert preview["errors"] and detail(world.client, "/staff/volunteers")["total"] == 0
    else:
        assert bool(preview["warnings"]) == legacy
        assert post(world.client, "/staff/volunteers/import/commit", preview=preview["preview"]).status_code == 200
        people = detail(world.client, "/staff/volunteers")["volunteers"]
        assert all(not p["rifle_qualified"] for p in people) if legacy else any(p["rifle_qualified"] for p in people)


def test_rifle_change_is_soft_and_never_leaks_to_volunteer_or_exports(world):
    person, pin, day, link, volunteer = setup(world)
    p = detail(world.client, f"/staff/volunteers/{person['id']}")["person"]
    assert post(world.client, f"/staff/volunteers/{p['id']}", expected_revision=p["revision"], display_name=p["display_name"], active=True, outside_available=True, rifle_qualified=True).status_code == 200
    world.app.state.generation.runner = Immediate()
    from phase2_helpers import save
    assert save(volunteer, link, first=True, second=True).status_code == 200
    assert generate(world, day).status_code == 200
    assert review(world, day)["optimization_current"]
    current = detail(world.client, f"/staff/volunteers/{p['id']}")["person"]
    assert post(world.client, f"/staff/volunteers/{p['id']}", expected_revision=current["revision"], display_name=p["display_name"], active=True, outside_available=True, rifle_qualified=False).status_code == 200
    data = review(world, day)
    assert not data["optimization_current"] and not data["problems"]
    assert "Previous solver results" in world.client.get(f"/staff/dates/{day['id']}/draft").text
    public = volunteer.get(f"/availability/{link}", headers={"Accept": "application/json"}).text
    own = volunteer.get(f"/availability/{link}/mine").text
    assert all(k not in public + own for k in ("rifle_qualified", "outside_available", "rifle_available"))
    assert post(volunteer, f"/staff/volunteers/{p['id']}", csrf="synthetic", expected_revision=3, display_name="Self", active=True, outside_available=True, rifle_qualified=True).status_code == 401
    assert approve(world, day).status_code == 200
    version = publish(world, day).json()["version_id"]
    root = f"/staff/dates/{day['id']}/publications/{version}"
    exports = world.client.get(root + "/signal.txt").text + "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(world.client.get(root + "/schedule.pdf").content)).pages)
    assert "Rifle" not in exports and "Outside Available" not in exports


@pytest.mark.parametrize("mutation", ["rifle", "zone"])
def test_rifle_and_zone_change_reject_inflight_commit(world, mutation):
    day, people = initial(world, 2)
    controlled = Controlled()
    world.app.state.generation.runner = controlled
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(generate, world, day)
        assert controlled.entered.wait(4)
        if mutation == "rifle":
            p = people[0]
            assert post(world.client, f"/staff/volunteers/{p['id']}", expected_revision=p["revision"], display_name=p["display_name"], active=True, outside_available=True, rifle_qualified=True).status_code == 200
        else:
            assert post(world.client, "/staff/settings", **settings_payload(2, inside_zone_label="Hall")).status_code == 200
        controlled.release.set()
        assert pending.result(timeout=5).status_code == 409
    assert review(world, day)["draft"] is None


@pytest.mark.parametrize("damage", ["rifle_score", "old_sequence", "false_proof", "rp1_score", "rp1_proof"])
def test_independent_commit_rejects_invalid_rifle_metadata(world, damage):
    day, _ = initial(world, 2)
    class Invalid:
        async def run(self, snapshot):
            result = solve(snapshot)
            meta = result["metadata"]
            if damage == "rifle_score":
                meta["objectives"]["RS"] = 2
            elif damage == "old_sequence":
                meta["completed_stages"] = ["E", "O", "B", "U"]
            elif damage == "rp1_score":
                meta["objectives"]["RP1"] = 2
            elif damage == "rp1_proof":
                meta["stages"][4]["status"] = "FEASIBLE"
            else:
                meta["stages"][2]["status"] = "FEASIBLE"
            return result
    world.app.state.generation.runner = Invalid()
    assert generate(world, day).status_code == 503
    assert review(world, day)["draft"] is None
