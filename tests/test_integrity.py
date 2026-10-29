import sqlite3
import uuid
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from scheduler.app import create_app
from scheduler.db import Database, audit, migrate, row
from conftest import PASSWORD, post, sign_in


def uid():
    return str(uuid.uuid4())


@pytest.fixture
def graph(world):
    dates, versions, people = [uid(), uid()], [uid(), uid()], [uid(), uid()]
    with world.db.transaction() as c:
        for person in people:
            c.exec_driver_sql("INSERT INTO volunteers(id,display_name,normalized_label,active,outside_available) VALUES(?,?,?,1,1)", (person, "Synthetic " + person, person))
        for index in range(2):
            c.exec_driver_sql("INSERT INTO service_dates(id,local_date,timezone_snapshot) VALUES(?,?,?)", (dates[index], ["2026-10-25", "2026-11-01"][index], "America/Los_Angeles"))
            c.exec_driver_sql("INSERT INTO schedule_versions(id,date_id,status,config_snapshot,config_epoch,creator) VALUES(?,?,'draft','{}',1,?)", (versions[index], dates[index], world.admin_id))
            for service in (1, 2):
                for position in range(1, 9):
                    c.exec_driver_sql("INSERT INTO slots(version_id,date_id,service_number,position_number,source,zone,essential) VALUES(?,?,?,?,'manual',?,1)", (versions[index], dates[index], service, position, "outside" if position in (1, 2, 3, 8) else "inside"))
        c.exec_driver_sql("INSERT INTO rotation_approvals(id,version_id,date_id,volunteer_id,first_position,second_position,first_revision,second_revision,first_fingerprint,second_fingerprint,reason,approving_user,approved_at) VALUES('rotation',?,?,?,1,2,1,1,'a','b','Synthetic reason',?,1)", (versions[0], dates[0], people[0], world.admin_id))
        c.exec_driver_sql("INSERT INTO vacancy_approvals(id,version_id,date_id,service_number,position_number,slot_revision,fingerprint,reason,approving_user,approved_at) VALUES('vacancy',?,?,1,3,1,'a','Synthetic reason',?,1)", (versions[0], dates[0], world.admin_id))
    return dates, versions, people


def test_pragmas_on_every_pool_connection_and_seed(db):
    connections = [db.engine.connect() for _ in range(5)]
    try:
        for c in connections:
            assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert c.exec_driver_sql("PRAGMA synchronous").scalar() == 2
            assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        settings = row(connections[0], "SELECT * FROM settings")
        assert settings["timezone"] == settings["service_1_label"] == settings["service_2_label"] == ""
        assert settings["essentiality_confirmed"] == 0
        assert settings["settings_revision"] == settings["config_epoch"] == settings["roster_epoch"] == 1
        positions = connections[0].exec_driver_sql("SELECT number,zone,restricted_required,essential FROM positions ORDER BY number").all()
        assert positions == [(p, "outside" if p in (1, 2, 3, 8) else "inside", int(p in (1, 2)), 1) for p in range(1, 9)]
    finally:
        for c in connections:
            c.close()


@pytest.mark.parametrize("case", ["slot_date", "base_date", "pointer_date", "rotation_date", "vacancy_date", "duplicate_draft", "duplicate_assignee", "service", "boolean", "role", "position", "audit_actor", "last_admin", "delete_actor", "timezone", "date_identity", "approval_revival", "active_label", "availability_boolean", "staff_confirmation", "session_actor", "link_date", "duplicate_link", "idempotency_date"])
def test_migrated_constraints(world, graph, case):
    dates, versions, people = graph
    statements = {
        "slot_date": ("UPDATE slots SET date_id=? WHERE version_id=?", (dates[1], versions[0])),
        "base_date": ("UPDATE schedule_versions SET base_version_id=? WHERE id=?", (versions[1], versions[0])),
        "pointer_date": ("UPDATE service_dates SET current_published_version_id=? WHERE id=?", (versions[1], dates[0])),
        "rotation_date": ("UPDATE rotation_approvals SET date_id=? WHERE id='rotation'", (dates[1],)),
        "vacancy_date": ("UPDATE vacancy_approvals SET date_id=? WHERE id='vacancy'", (dates[1],)),
        "duplicate_draft": ("INSERT INTO schedule_versions(id,date_id,status,config_snapshot,config_epoch,creator) VALUES(?,?,'draft','{}',1,?)", (uid(), dates[0], world.admin_id)),
        "duplicate_assignee": ("UPDATE slots SET volunteer_id=? WHERE version_id=? AND service_number=1", (people[0], versions[0])),
        "service": ("UPDATE slots SET service_number=3 WHERE version_id=?", (versions[0],)),
        "boolean": ("UPDATE volunteers SET active=3 WHERE id=?", (people[0],)),
        "role": ("UPDATE admin_users SET role='root' WHERE id=?", (world.scheduler_id,)),
        "position": ("UPDATE positions SET zone='inside' WHERE number=1", ()),
        "audit_actor": ("INSERT INTO audit_events VALUES('bad',1,'action','obj','staff',NULL,NULL,'{}')", ()),
        "last_admin": ("UPDATE admin_users SET active=0 WHERE id=?", (world.admin_id,)),
        "delete_actor": ("DELETE FROM admin_users WHERE id=?", (world.admin_id,)),
        "timezone": ("UPDATE service_dates SET timezone_snapshot='UTC' WHERE id=?", (dates[0],)),
        "date_identity": ("UPDATE service_dates SET local_date='2026-11-08' WHERE id=?", (dates[0],)),
        "approval_revival": ("UPDATE vacancy_approvals SET invalidated_at=NULL WHERE id='vacancy'", ()),
        "active_label": ("UPDATE volunteers SET normalized_label=? WHERE id=?", (people[0], people[1])),
        "availability_boolean": ("INSERT INTO availability(date_id,volunteer_id,service_1_available,service_2_available,revision,source) VALUES(?,?,2,1,1,'volunteer')", (dates[0], people[0])),
        "staff_confirmation": ("INSERT INTO availability(date_id,volunteer_id,service_1_available,service_2_available,revision,source) VALUES(?,?,1,1,1,'admin')", (dates[0], people[0])),
        "session_actor": ("INSERT INTO staff_sessions VALUES('hash','unknown','staff',1,1,1,2,NULL,'csrf')", ()),
        "link_date": ("INSERT INTO volunteer_sessions VALUES('hash',?,?,'link',1,1,1,1,2,NULL,'csrf')", (people[0], dates[1])),
        "duplicate_link": ("INSERT INTO date_links VALUES('link2',?,'digest2',2,1,2,NULL,?)", (dates[0], world.admin_id)),
        "idempotency_date": ("INSERT INTO idempotency VALUES(?,?,'publish','key','digest',?,1)", (world.admin_id, dates[0], versions[1])),
    }
    with world.db.transaction() as c:
        c.exec_driver_sql("UPDATE vacancy_approvals SET invalidated_at=1 WHERE id='vacancy'")
        c.exec_driver_sql("INSERT INTO date_links VALUES('link',?,'digest',1,1,2,NULL,?)", (dates[0], world.admin_id))
    with pytest.raises(IntegrityError), world.db.transaction() as c:
        c.exec_driver_sql(*statements[case])


@pytest.mark.parametrize("date", ["2026-10-26", "xxxxxxxxxx", "2026-02-30", "2026-10-00", "not-a-date"])
def test_invalid_sundays_rejected(db, date):
    with pytest.raises(IntegrityError), db.transaction() as c:
        c.exec_driver_sql("INSERT INTO service_dates(id,local_date,timezone_snapshot) VALUES(?,?,'UTC')", (uid(), date))


def publish_sql(c, version, actor, number=1):
    # Schema fixture only. This intentionally does not stand in for a future publication service.
    c.exec_driver_sql("UPDATE schedule_versions SET status='published',publication_number=?,published_actor=?,published_at=1,published_payload='{}',payload_hash='hash' WHERE id=?", (number, actor, version))


@pytest.mark.parametrize("table", ["slots", "rotation_approvals", "vacancy_approvals"])
@pytest.mark.parametrize("operation", ["insert", "update", "delete", "move_into"])
def test_published_child_guards(world, graph, table, operation):
    dates, versions, people = graph
    with world.db.transaction() as c:
        if operation == "move_into" and table != "slots":
            if table == "rotation_approvals":
                c.exec_driver_sql("INSERT INTO rotation_approvals SELECT 'moving',?,?,volunteer_id,first_service,first_position,second_service,second_position,first_revision,second_revision,first_fingerprint,second_fingerprint,reason,approving_user,approved_at,invalidated_at FROM rotation_approvals", (versions[1], dates[1]))
            else:
                c.exec_driver_sql("INSERT INTO vacancy_approvals SELECT 'moving',?,?,service_number,position_number,slot_revision,fingerprint,reason,approving_user,approved_at,invalidated_at FROM vacancy_approvals", (versions[1], dates[1]))
        publish_sql(c, versions[0], world.admin_id)
    with pytest.raises(IntegrityError), world.db.transaction() as c:
        if operation == "delete":
            c.exec_driver_sql(f"DELETE FROM {table} WHERE version_id=?", (versions[0],))
        elif operation == "update":
            c.exec_driver_sql(f"UPDATE {table} SET version_id=version_id WHERE version_id=?", (versions[0],))
        elif operation == "move_into":
            if table == "slots":
                c.exec_driver_sql("UPDATE slots SET version_id=?,date_id=? WHERE version_id=?", (versions[0], dates[0], versions[1]))
            else:
                # The trigger checks NEW parent as well as OLD, even if a row is moved.
                c.exec_driver_sql(f"UPDATE {table} SET version_id=?,date_id=? WHERE id='moving'", (versions[0], dates[0]))
        elif table == "slots":
            c.exec_driver_sql("INSERT INTO slots(version_id,date_id,service_number,position_number,source,zone,essential) VALUES(?,?,1,1,'manual','outside',1)", (versions[0], dates[0]))
        elif table == "rotation_approvals":
            c.exec_driver_sql("INSERT INTO rotation_approvals SELECT 'new',version_id,date_id,volunteer_id,first_service,first_position,second_service,second_position,first_revision,second_revision,first_fingerprint,second_fingerprint,reason,approving_user,approved_at,invalidated_at FROM rotation_approvals")
        else:
            c.exec_driver_sql("INSERT INTO vacancy_approvals SELECT 'new',version_id,date_id,service_number,position_number,slot_revision,fingerprint,reason,approving_user,approved_at,invalidated_at FROM vacancy_approvals")


def test_publication_guards_number_and_first_publication(world, graph):
    dates, versions, _ = graph
    with world.db.transaction() as c:
        publish_sql(c, versions[0], world.admin_id)
        c.exec_driver_sql("UPDATE service_dates SET current_published_version_id=?,first_published_at=1,availability_open=0 WHERE id=?", (versions[0], dates[0]))
        c.exec_driver_sql("INSERT INTO schedule_versions(id,date_id,status,config_snapshot,config_epoch,creator) VALUES('next',?,'draft','{}',1,?)", (dates[0], world.admin_id))
    for sql, args in [
        ("UPDATE schedule_versions SET status='draft' WHERE id=?", (versions[0],)),
        ("DELETE FROM schedule_versions WHERE id=?", (versions[0],)),
        ("UPDATE service_dates SET first_published_at=NULL WHERE id=?", (dates[0],)),
        ("UPDATE service_dates SET availability_open=1 WHERE id=?", (dates[0],)),
    ]:
        with pytest.raises(IntegrityError), world.db.transaction() as c:
            c.exec_driver_sql(sql, args)
    with pytest.raises(IntegrityError), world.db.transaction() as c:
        publish_sql(c, "next", world.admin_id)


def test_audit_append_only_and_mutation_rollback(world):
    sign_in(world)
    with world.db.transaction() as c:
        c.exec_driver_sql("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events WHEN NEW.action='staff.created' BEGIN SELECT RAISE(ABORT,'injected'); END")
    result = post(world.client, "/staff/users", username="must-rollback", password=PASSWORD, role="scheduler")
    assert result.status_code == 409
    assert "injected" not in result.text and PASSWORD not in result.text
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM admin_users WHERE username='must-rollback'").scalar() == 0
    for sql in ("UPDATE audit_events SET action='tampered'", "DELETE FROM audit_events"):
        with pytest.raises(IntegrityError), world.db.transaction() as c:
            c.exec_driver_sql(sql)


def test_concurrent_last_admin_protection(world):
    with world.db.transaction() as c:
        c.exec_driver_sql("UPDATE admin_users SET role='admin' WHERE id=?", (world.scheduler_id,))
    def deactivate(user_id):
        try:
            with world.db.transaction() as c:
                c.exec_driver_sql("UPDATE admin_users SET active=0 WHERE id=?", (user_id,))
                audit(c, "test.deactivated", user_id)
            return "ok"
        except IntegrityError:
            return "conflict"
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(deactivate, [world.admin_id, world.scheduler_id]))
    assert sorted(results) == ["conflict", "ok"]
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM admin_users WHERE role='admin' AND active=1").scalar() == 1


def test_busy_database_is_503(world):
    sign_in(world)
    world.db.engine.dispose()
    busy_db = Database(world.db.path, busy_ms=20)
    app = create_app(busy_db, world.security, "https://testserver", lambda: world.now[0])
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.update(world.client.cookies)
        with world.db.transaction():
            result = client.get("/staff")
        assert result.status_code == 503 and result.headers["Retry-After"] == "5"
        assert "SELECT" not in result.text


def test_migration_backup_and_restart(world):
    world.db.engine.dispose()
    migrate(world.db.path)
    backups = list(Path(world.db.path).parent.glob("*.pre-migration-*.sqlite"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT count(*) FROM admin_users").fetchone()[0] == 2
    fresh = Database(world.db.path)
    fresh.check()
    fresh.engine.dispose()


def test_startup_fails_with_missing_key_or_wrong_schema(db, security, tmp_path, monkeypatch):
    monkeypatch.setenv("TOTP_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError), TestClient(create_app(db, origin="https://testserver")):
        pass
    with db.transaction() as c:
        c.exec_driver_sql("UPDATE alembic_version SET version_num='wrong'")
    with pytest.raises(RuntimeError, match="Schema mismatch"), TestClient(create_app(db, security, "https://testserver")):
        pass
