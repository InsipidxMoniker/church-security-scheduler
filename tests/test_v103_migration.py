"""Upgrade/restore rehearsal from a populated 0005 database, never production."""
import json
import shutil
import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.config import Config

from scheduler.auth import Auth
from scheduler.backup import restore_database, verify
from scheduler.db import Database, HEAD, migrate
from scheduler.exports import pdf_bytes, signal_text
from scheduler.publication import canonical, sha
from conftest import PASSWORD
from solver_helpers import snapshot


def tables(path):
    with closing(sqlite3.connect(path)) as c:
        c.row_factory = sqlite3.Row
        return {r[0]: [dict(row) for row in c.execute('SELECT * FROM "' + r[0] + '" ORDER BY rowid')]
                for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def test_upgrade_preserves_all_state_and_prebackup_restore(tmp_path, security):
    path = tmp_path / "old.sqlite"
    cfg = Config("alembic.ini")
    cfg.attributes["database_path"] = str(path)
    command.upgrade(cfg, "0005_publication_guards")
    db = Database(path)
    actor = Auth(db, security).bootstrap("migration-admin", PASSWORD)
    fixture = snapshot(2, 1)
    person = fixture["volunteers"][0]["id"]
    with db.transaction() as c:
        for v in fixture["volunteers"]:
            name = "Synthetic " + v["id"]
            c.exec_driver_sql("INSERT INTO volunteers(id,display_name,public_label,normalized_label,search_text,active,restricted_qualified,pin_hash) VALUES(?,?,?,?,?,1,?,?)",
                              (v["id"], name, name, name, name, int(v["outside_available"]), security.hash_password("01234567")))
            c.exec_driver_sql("INSERT INTO volunteer_pin_secrets(volunteer_id,pin_ciphertext) VALUES(?,?)", (v["id"], security.cipher.encrypt(b"synthetic saved PIN").decode()))
        c.exec_driver_sql("INSERT INTO service_dates(id,local_date,timezone_snapshot) VALUES('date','2026-10-25','America/Los_Angeles')")
        c.exec_driver_sql("INSERT INTO service_dates(id,local_date,timezone_snapshot) VALUES('draft-date','2026-11-01','America/Los_Angeles')")
        c.exec_driver_sql("INSERT INTO availability(date_id,volunteer_id,service_1_available,service_2_available,revision,source,actor_id,confirmation_reason) VALUES('date',?,1,1,1,'admin',?,'Confirmed')", (person, actor))
        for vid in ("published", "draft"):
            date_id = "date" if vid == "published" else "draft-date"
            c.exec_driver_sql("INSERT INTO schedule_versions(id,date_id,status,config_snapshot,config_epoch,creator) VALUES(?,?,'draft',?,1,?)", (vid, date_id, canonical(fixture["config"]), actor))
            for s in fixture["slots"]:
                c.exec_driver_sql("INSERT INTO slots(version_id,date_id,service_number,position_number,volunteer_id,source,zone,essential,locked) VALUES(?,?,?,?,?,'manual',?,?,?)", (vid, date_id, s["service_number"], s["position_number"], person if s["service_number"] == 1 and s["position_number"] == 1 else None, s["zone"], int(s["essential"]), int(s["position_number"] == 1)))
        c.exec_driver_sql("INSERT INTO vacancy_approvals(id,version_id,date_id,service_number,position_number,slot_revision,fingerprint,reason,approving_user,approved_at) VALUES('approval','draft','draft-date',1,2,1,'fingerprint','Reason',?,1)", (actor,))
        payload = {"config": fixture["config"], "slots": [{**s, "display_name": None, "vacancy_approved": False, "rotation_approved": False} for s in fixture["slots"]],
                   "publication_number": 1, "base_version_id": None, "local_date": fixture["local_date"], "timezone": "America/Los_Angeles"}
        encoded = canonical(payload)
        c.exec_driver_sql("UPDATE schedule_versions SET status='published',publication_number=1,published_actor=?,published_at=1,published_payload=?,payload_hash=? WHERE id='published'", (actor, encoded, sha(encoded)))
        c.exec_driver_sql("UPDATE service_dates SET availability_open=0,first_published_at=1,current_published_version_id='published' WHERE id='date'")
    db.engine.dispose()
    before = tables(path)
    before_signal = signal_text(payload)
    before_pdf = pdf_bytes(payload)
    migrate(path)
    after = tables(path)
    assert after["alembic_version"][0]["version_num"] == HEAD
    for name, rows in before.items():
        if name not in ("volunteers", "settings", "alembic_version"):
            assert after[name] == rows, name
    for old, new in zip(before["volunteers"], after["volunteers"]):
        assert new == {**{k: v for k, v in old.items() if k != "restricted_qualified"}, "outside_available": old["restricted_qualified"], "rifle_qualified": 0}
    assert after["settings"][0] == {**before["settings"][0], "inside_zone_label": "Inside", "outside_zone_label": "Outside", "config_epoch": 2, "settings_revision": 2}
    assert signal_text(json.loads(after["schedule_versions"][0]["published_payload"])) == before_signal
    from pypdf import PdfReader
    import io
    extract = lambda data: [p.extract_text() for p in PdfReader(io.BytesIO(data)).pages]
    assert extract(pdf_bytes(payload)) == extract(before_pdf)
    verify(path)
    with closing(sqlite3.connect(path)) as c:
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
        for sql in ("UPDATE volunteers SET rifle_qualified=1 WHERE outside_available=0", "UPDATE volunteers SET rifle_qualified=2", "UPDATE volunteers SET rifle_qualified=NULL", "UPDATE volunteers SET rifle_qualified=1,outside_available=0"):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO volunteers(id,display_name,normalized_label,public_label,search_text,active,outside_available,rifle_qualified) VALUES('bad','Bad','bad','Bad','bad',1,0,1)")
    backups = list(tmp_path.glob("old.sqlite.pre-migration-*.sqlite"))
    assert len(backups) == 1 and tables(backups[0]) == before
    # Original prebackup remains usable with the previous application. For the
    # current restore tool migrate an OFFLINE COPY, preserving the original.
    with pytest.raises(RuntimeError, match="schema must match"):
        verify(backups[0])
    recovery = tmp_path / "recovery-copy.sqlite"
    shutil.copyfile(backups[0], recovery)
    migrate(recovery)
    restored = tmp_path / "restored.sqlite"
    restore_database(recovery, restored, tmp_path / "external-key")
    verify(restored)
    recovered = tables(restored)
    assert recovered["volunteers"] == after["volunteers"]
    assert recovered["schedule_versions"] == after["schedule_versions"]
    assert recovered["admin_users"] == after["admin_users"]
    assert all(s["revoked_at"] is not None for s in recovered["staff_sessions"])
    assert recovered["service_dates"][0]["availability_open"] == 0
    assert tables(backups[0]) == before


def test_failed_upgrade_rolls_back_schema_and_keeps_verified_prebackup(tmp_path, monkeypatch):
    path = tmp_path / "interrupted.sqlite"
    cfg = Config("alembic.ini")
    cfg.attributes["database_path"] = str(path)
    command.upgrade(cfg, "0005_publication_guards")
    before = tables(path)
    with closing(sqlite3.connect(path)) as c:
        schema = c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    from alembic import op
    execute = op.execute
    def fail_after_rename(statement, *args, **kwargs):
        if "ADD COLUMN rifle_qualified" in statement:
            raise RuntimeError("Synthetic migration interruption")
        return execute(statement, *args, **kwargs)
    monkeypatch.setattr(op, "execute", fail_after_rename)
    with pytest.raises(RuntimeError, match="Synthetic migration interruption"):
        migrate(path)
    assert tables(path) == before
    with closing(sqlite3.connect(path)) as c:
        assert c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall() == schema
    backups = list(tmp_path.glob("interrupted.sqlite.pre-migration-*.sqlite"))
    assert len(backups) == 1 and tables(backups[0]) == before
