"""Operator-only consistent SQLite backup and fail-closed clean restore."""
import os
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import closing

from scheduler.db import Database, audit, HEAD
from scheduler.publication import Publication


def verify(path):
    with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or c.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Database integrity verification failed")
        if c.execute("SELECT version_num FROM alembic_version").fetchone()[0] != HEAD:
            raise RuntimeError("Restore schema must match this application; migrate an offline copy first")
        for version in c.execute("SELECT * FROM schedule_versions WHERE status='published'"):
            Publication.decode(dict(version))


def backup_database(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise RuntimeError("Source database does not exist")
    if destination.exists():
        raise FileExistsError(destination)
    temporary = destination.with_name(destination.name + ".tmp-" + str(uuid.uuid4()))
    with temporary.open("xb"):
        pass
    temporary.chmod(0o600)
    try:
        with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original, closing(sqlite3.connect(temporary)) as target:
            original.backup(target, pages=256, sleep=.05)
        verify(temporary)
        with temporary.open("rb") as completed:
            os.fsync(completed.fileno())
        # Atomic no-overwrite publication of a verified file on the same filesystem.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def restore_database(source, destination, key_path):
    """Only a new path is accepted. Stop the app/proxy before switching volumes."""
    from scheduler.security import Security
    target = Path(destination)
    if target.exists() or Path(str(target) + "-wal").exists() or Path(str(target) + "-shm").exists():
        raise RuntimeError("Restore requires a clean destination; existing data is never overwritten")
    verify(source)
    staged = target.with_name(target.name + ".restoring-" + str(uuid.uuid4()))
    backup_database(source, staged)
    try:
        db = Database(staged)
        try:
            security = Security(key_path)
            with db.transaction() as c:
                # Verify key recovery against all existing staff seeds before opening.
                for user in c.exec_driver_sql("SELECT * FROM admin_users").mappings():
                    security.seed(dict(user))
                now = time.time()
                c.exec_driver_sql("UPDATE staff_sessions SET revoked_at=? WHERE revoked_at IS NULL", (now,))
                c.exec_driver_sql("UPDATE volunteer_sessions SET revoked_at=? WHERE revoked_at IS NULL", (now,))
                c.exec_driver_sql("UPDATE date_links SET revoked_at=? WHERE revoked_at IS NULL", (now,))
                c.exec_driver_sql("DELETE FROM date_link_secrets")
                c.exec_driver_sql("UPDATE service_dates SET availability_open=0,date_input_epoch=date_input_epoch+1")
                audit(c, "system.restore_quarantined", "database", details={"sessions_revoked": True, "links_revoked": True, "collection_closed": True})
        finally:
            db.engine.dispose()
        verify(staged)
        with staged.open("rb") as completed:
            os.fsync(completed.fileno())
        os.link(staged, target)
    except BaseException:
        raise RuntimeError("Restore failed verification; no restored destination was published") from None
    finally:
        staged.unlink(missing_ok=True)
