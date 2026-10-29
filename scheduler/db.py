import json
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event

HEAD = "0006_qualification_zones"


class Database:
    def __init__(self, path=None, busy_ms=5000):
        self.path = str(path or os.environ.get("DATABASE_PATH", "/data/scheduler.db"))
        self.engine = create_engine(
            "sqlite:///" + self.path,
            connect_args={"check_same_thread": False, "isolation_level": None},
            pool_size=5, max_overflow=0,
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")

    @contextmanager
    def transaction(self):
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def check(self):
        with self.engine.connect() as c:
            if c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() != HEAD:
                raise RuntimeError("Schema mismatch; run the migration command")
            if c.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
                raise RuntimeError("Foreign keys are disabled")
            c.exec_driver_sql("SELECT id FROM settings WHERE id=1").one()


def row(c, sql, args=()):
    result = c.exec_driver_sql(sql, args).mappings().first()
    return dict(result) if result else None


def audit(c, action, object_id, *, actor=None, volunteer=None, details=None):
    # Callers supply allowlisted, non-secret metadata, never request bodies.
    c.exec_driver_sql(
        "INSERT INTO audit_events(id,occurred_at,action,object_id,actor_type,staff_id,volunteer_id,details) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), time.time(), action, str(object_id),
         "staff" if actor else ("volunteer" if volunteer else "system"), actor, volunteer, json.dumps(details or {}, sort_keys=True)),
    )


def migrate(path=None):
    from alembic import command
    from alembic.config import Config

    path = Path(path or os.environ.get("DATABASE_PATH", "/data/scheduler.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        target = path.with_name(f"{path.name}.pre-migration-{time.time_ns()}.sqlite")
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as backup:
            source.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Pre-migration backup failed integrity check")
        target.chmod(0o600)
    cfg = Config("alembic.ini")
    cfg.attributes["database_path"] = str(path)
    command.upgrade(cfg, "head")
    path.chmod(0o600)
