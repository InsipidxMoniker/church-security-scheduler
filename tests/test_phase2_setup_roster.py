import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from scheduler.db import HEAD, Database, row, migrate
from scheduler.app import CSRF
from conftest import sign_in, post
from phase2_helpers import create_person, detail, new_link, open_date, settings_payload, setup


def test_settings_confirmation_fixed_positions_and_epochs(world):
    sign_in(world)
    unconfigured = detail(world.client, "/staff/settings")
    assert unconfigured["timezone"] == "" and unconfigured["essentiality_confirmed"] == 0
    assert post(world.client, "/staff/dates", local_date="2026-10-25").status_code == 409
    saved = post(world.client, "/staff/settings", **settings_payload(essential_8=False, essentiality_confirmed=False)).json()
    assert saved["settings_revision"] == 2 and saved["config_epoch"] == 2
    assert saved["service_1_time"] is saved["service_2_time"] is None
    assert [(p["number"], p["zone"], p["restricted_required"]) for p in saved["positions"]] == [(i, "outside" if i in (1,2,3,8) else "inside", int(i in (1,2))) for i in range(1,9)]
    assert "Setup incomplete" in world.client.get("/staff").text
    confirmed = post(world.client, "/staff/settings", **settings_payload(2, essential_8=False)).json()
    assert confirmed["config_epoch"] == 2 and confirmed["settings_revision"] == 3 and confirmed["essentiality_confirmed"] == 1
    assert "Setup incomplete" not in world.client.get("/staff").text


def test_timezone_only_snapshot_and_old_draft_preservation(world):
    sign_in(world)
    post(world.client, "/staff/settings", **settings_payload())
    day = open_date(world.client)
    # Persistence fixture only: no draft-creation/publication workflow in Phase 2.
    with world.db.transaction() as c:
        c.exec_driver_sql("INSERT INTO schedule_versions(id,date_id,status,config_snapshot,config_epoch,creator) VALUES('snapshot',?,'draft',?,2,?)", (day["id"], json.dumps({"timezone": day["timezone_snapshot"]}), world.admin_id))
    saved = post(world.client, "/staff/settings", **settings_payload(2, timezone="Pacific/Auckland")).json()
    assert saved["settings_revision"] == 3 and saved["config_epoch"] == 2
    assert open_date(world.client)["id"] == day["id"]
    assert detail(world.client, f"/staff/dates/{day['id']}")["day"]["timezone_snapshot"] == "America/Los_Angeles"
    assert open_date(world.client, "2026-11-01")["timezone_snapshot"] == "Pacific/Auckland"
    with world.db.engine.connect() as c:
        draft = row(c, "SELECT * FROM schedule_versions WHERE id='snapshot'")
        assert json.loads(draft["config_snapshot"])["timezone"] == "America/Los_Angeles" and draft["config_epoch"] == 2
    change = post(world.client, "/staff/settings", **settings_payload(3, timezone="Pacific/Auckland", service_1_label="Early service", service_1_time="09:15", essential_3=False)).json()
    assert change["config_epoch"] == 3
    assert post(world.client, "/staff/settings", **settings_payload(3)).status_code == 409


@pytest.mark.parametrize("change", [{"timezone":"Made/Up"},{"timezone":"../UTC"},{"service_1_label":" \t"},{"service_2_label":"bad\nlabel"},{"service_1_time":"25:00"},{"pin_digits":3},{"pin_digits":13},{"link_days":0},{"zone":"inside"}])
def test_invalid_settings_no_mutation(world, change):
    sign_in(world)
    result = post(world.client, "/staff/settings", **settings_payload(**change))
    assert result.status_code == 422
    assert detail(world.client, "/staff/settings")["settings_revision"] == 1


@pytest.mark.parametrize("name", ["", " \t ", "A\nB", "\nName", "A\x00B", "A\x85B", "x"*101])
def test_invalid_names(world, name):
    sign_in(world)
    result = post(world.client, "/staff/volunteers", display_name=name, public_label="", active=True, restricted_qualified=False)
    assert result.status_code == 422


def test_duplicate_names_public_labels_unicode_and_noop(world):
    sign_in(world)
    first = create_person(world.client, "Same Name")
    duplicate = post(world.client, "/staff/volunteers", display_name=" same name ", public_label="", active=True, restricted_qualified=False)
    assert duplicate.status_code == 409
    distinct = post(world.client, "/staff/volunteers", display_name="Same Name", public_label="Same Name B", active=True, restricted_qualified=True).json()
    assert distinct["id"] != first["id"] and distinct["display_name"] == first["display_name"]
    street = create_person(world.client, "Straße")
    assert post(world.client, "/staff/volunteers", display_name="STRASSE", active=True, restricted_qualified=False).status_code == 409
    assert detail(world.client, "/staff/volunteers?q=STRASSE")["volunteers"][0]["id"] == street["id"]
    rtl = create_person(world.client, "متطوع 合成 <script>")
    assert "&lt;script&gt;" in world.client.get(f"/staff/volunteers/{rtl['id']}").text
    before = detail(world.client, "/staff/settings")["roster_epoch"]
    no_op = post(world.client, f"/staff/volunteers/{first['id']}", expected_revision=1, display_name=first["display_name"], public_label=first["public_label"], active=True, restricted_qualified=False)
    assert no_op.json()["revision"] == 1 and detail(world.client, "/staff/settings")["roster_epoch"] == before


def test_activation_deactivation_and_scheduler_rights(world):
    sign_in(world, "synthetic-scheduler")
    person = create_person(world.client)
    assert person["has_pin"] == 0
    for suffix, data in [("pin", {"expected_revision":1}), ("clear-block", {})]:
        assert post(world.client, f"/staff/volunteers/{person['id']}/{suffix}", **data).status_code == 403
    assert world.client.get("/staff/settings").status_code == 403
    assert post(world.client, "/staff/settings", **settings_payload()).status_code == 403
    edited = post(world.client, f"/staff/volunteers/{person['id']}", expected_revision=1, display_name=person["display_name"], public_label="Distinct A", active=False, restricted_qualified=True).json()
    assert edited["active"] == 0 and edited["revision"] == 2
    assert post(world.client, f"/staff/volunteers/{person['id']}", expected_revision=1, display_name="Lost update", active=True, restricted_qualified=False).status_code == 409
    assert world.client.delete(f"/staff/volunteers/{person['id']}", headers={"Origin":"https://testserver"}).status_code == 405


def test_csv_large_roster_pagination_and_atomic_preview(world):
    sign_in(world)
    text = "display_name,restricted_qualified\n" + "\n".join(f"Synthetic {i:03d},{'true' if i%3==0 else 'false'}" for i in range(130))
    preview = post(world.client, "/staff/volunteers/import/preview", csv_text=text).json()
    assert not preview["errors"] and len(preview["rows"]) == 130
    assert detail(world.client, "/staff/volunteers")["total"] == 0
    commit = post(world.client, "/staff/volunteers/import/commit", preview=preview["preview"])
    assert commit.json() == {"created":130}
    assert post(world.client, "/staff/volunteers/import/commit", preview=preview["preview"]).status_code == 409
    pages = [detail(world.client, f"/staff/volunteers?page={i}") for i in (1,2,3)]
    assert [len(p["volunteers"]) for p in pages] == [50,50,30]
    assert len({v["id"] for p in pages for v in p["volunteers"]}) == 130
    assert all(v["has_pin"] == 0 for p in pages for v in p["volunteers"])
    assert detail(world.client, "/staff/volunteers?q=129")["total"] == 1
    assert world.client.get("/staff/volunteers?page_size=101").status_code == 422
    assert world.client.get("/staff/volunteers?page=0").status_code == 422


@pytest.mark.parametrize("text", ["display_name,pin\nA,12345678", "display_name,restricted_qualified\nA,TRUE", "display_name,restricted_qualified\nA,false\n a ,true", "display_name,restricted_qualified\nGood,true\nBad,false,extra", "display_name,restricted_qualified\n\"bad", "display_name,restricted_qualified\n"])
def test_invalid_csv_no_commit(world, text):
    sign_in(world)
    preview = post(world.client, "/staff/volunteers/import/preview", csv_text=text).json()
    assert preview["errors"] and preview["preview"] is None
    assert detail(world.client, "/staff/volunteers")["total"] == 0


def test_csv_preview_actor_epoch_tamper_and_transaction_rollback(world):
    sign_in(world)
    csv_text = "display_name,restricted_qualified\nCSV One,true\nCSV Two,false"
    preview = post(world.client, "/staff/volunteers/import/preview", csv_text=csv_text).json()["preview"]
    other = TestClient(world.app, base_url="https://testserver")
    sign_in(world, "synthetic-scheduler", other)
    assert post(other, "/staff/volunteers/import/commit", preview=preview).status_code == 409
    assert post(world.client, "/staff/volunteers/import/commit", preview=preview[:-4]+"AAAA").status_code == 409
    create_person(world.client, "New during preview")
    assert post(world.client, "/staff/volunteers/import/commit", preview=preview).status_code == 409
    preview = post(world.client, "/staff/volunteers/import/preview", csv_text=csv_text).json()["preview"]
    before = detail(world.client, "/staff/settings")["roster_epoch"]
    with world.db.transaction() as c:
        c.exec_driver_sql("CREATE TRIGGER stop_import BEFORE INSERT ON audit_events WHEN NEW.action='roster.imported' BEGIN SELECT RAISE(ABORT,'injected'); END")
    assert post(world.client, "/staff/volunteers/import/commit", preview=preview).status_code == 409
    assert detail(world.client, "/staff/volunteers")["total"] == 1
    assert detail(world.client, "/staff/settings")["roster_epoch"] == before


@pytest.mark.parametrize("prefix", ["", "\ufeff"])
def test_utf8_file_import_and_formula_like_names(world, prefix):
    sign_in(world)
    headers = {"Origin":"https://testserver","Accept":"application/json"}
    data = {"csrf": world.client.cookies[CSRF]}
    bad = world.client.post("/staff/volunteers/import/preview", data=data, files={"csv_file":("bad.csv",b"\xff", "text/csv")}, headers=headers)
    assert bad.status_code == 422
    valid = world.client.post("/staff/volunteers/import/preview", data=data, files={"csv_file":("roster.csv", (prefix+"display_name,restricted_qualified\n=literal,true\n合成,false").encode(), "text/csv")}, headers=headers)
    assert valid.status_code == 200 and not valid.json()["errors"]
    assert post(world.client, "/staff/volunteers/import/commit", preview=valid.json()["preview"]).json()["created"] == 2


def test_forward_upgrade_preserves_phase1_data(tmp_path):
    path = tmp_path / "upgrade.sqlite"
    cfg = Config("alembic.ini")
    cfg.attributes["database_path"] = str(path)
    command.upgrade(cfg, "0001_foundation")
    with closing(sqlite3.connect(path)) as c:
        c.execute("INSERT INTO volunteers(id,display_name,normalized_label,active,restricted_qualified) VALUES('existing','合成 Existing','合成 existing',1,1)")
        c.commit()
    migrate(path)
    db = Database(path)
    with db.engine.connect() as c:
        person = row(c, "SELECT * FROM volunteers WHERE id='existing'")
        assert person["public_label"] == "合成 Existing" and person["search_text"] == "合成 existing"
        assert person["revision"] == 1
    db.check()
    db.engine.dispose()
    assert list(tmp_path.glob("*.pre-migration-*.sqlite"))


@pytest.mark.parametrize("kind", ["settings","roster","open","link"])
def test_actual_concurrent_cas_and_idempotency(world, kind):
    person, _, day, _, _ = setup(world)
    clients = [TestClient(world.app, base_url="https://testserver") for _ in range(2)]
    for client in clients:
        client.cookies.update(world.client.cookies)
    start = threading.Barrier(2)
    def change(index):
        start.wait(timeout=5)
        client = clients[index]
        if kind == "settings":
            return post(client, "/staff/settings", **settings_payload(2, timezone=["UTC","Pacific/Auckland"][index]))
        if kind == "roster":
            return post(client, f"/staff/volunteers/{person['id']}", expected_revision=person["revision"], display_name=f"Changed {index}", public_label="", active=True, restricted_qualified=bool(index))
        if kind == "open":
            return post(client, "/staff/dates", local_date="2026-11-01")
        return post(client, f"/staff/dates/{day['id']}/link", expected_epoch=day["date_input_epoch"], operation="rotate")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(change, (0,1)))
    if kind == "open":
        assert [r.status_code for r in results] == [200,200]
        assert results[0].json()["id"] == results[1].json()["id"]
    else:
        assert sorted(r.status_code for r in results) == [200,409]
    with world.db.engine.connect() as c:
        if kind == "settings":
            assert row(c, "SELECT * FROM settings")["settings_revision"] == 3
            assert row(c, "SELECT * FROM settings")["config_epoch"] == 2
        if kind == "roster":
            assert row(c, "SELECT * FROM volunteers WHERE id=?", (person["id"],))["revision"] == person["revision"] + 1
