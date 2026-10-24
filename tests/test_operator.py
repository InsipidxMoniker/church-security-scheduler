import os
import subprocess
import sys

from scheduler.db import row
from scheduler.security import Security
from conftest import PASSWORD


def invoke(environment, *args, password=None):
    return subprocess.run([sys.executable, "-m", "scheduler.manage", *args],
                          input=(password + "\n") * 2 if password else None,
                          text=True, capture_output=True, env=environment, timeout=15)


def test_real_local_cli_bootstrap_recovery_and_key_protection(db, tmp_path):
    key = tmp_path / "separate-keys" / "totp.key"
    environment = {**os.environ, "DATABASE_PATH": db.path, "TOTP_KEY_FILE": str(key)}
    assert invoke(environment, "init-key").returncode == 0
    original = key.read_bytes()
    assert invoke(environment, "init-key").returncode == 1
    assert key.read_bytes() == original
    assert key.stat().st_mode & 0o777 == 0o600
    first = invoke(environment, "bootstrap", "--username", "synthetic-cli", password=PASSWORD)
    assert first.returncode == 0, first.stderr
    assert PASSWORD not in first.stdout + first.stderr
    assert invoke(environment, "bootstrap", "--username", "another", password=PASSWORD).returncode == 1
    recovered = invoke(environment, "recover", "--username", "synthetic-cli", password=PASSWORD + " changed")
    assert recovered.returncode == 0, recovered.stderr
    with db.engine.connect() as c:
        user = row(c, "SELECT * FROM admin_users WHERE username='synthetic-cli'")
        assert user["credential_epoch"] == 2 and user["enrolled"] == 0
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE action='staff.local_recovery'").scalar() == 1
    security = Security(key)
    assert security.verify(user["password_hash"], PASSWORD + " changed")
    assert not security.verify(user["password_hash"], PASSWORD)


def test_cli_migrate_takes_backup(db, tmp_path):
    result = invoke({**os.environ, "DATABASE_PATH": db.path}, "migrate")
    assert result.returncode == 0, result.stderr
    assert len(list(tmp_path.glob("*.pre-migration-*.sqlite"))) == 1
