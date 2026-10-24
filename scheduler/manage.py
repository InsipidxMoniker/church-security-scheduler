"""Local-only management. Passwords use getpass; never command-line arguments."""
import argparse
import getpass
import os
from pathlib import Path

from cryptography.fernet import Fernet

from scheduler.auth import Auth
from scheduler.db import Database, migrate
from scheduler.security import Problem, Security


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-key", "migrate", "bootstrap", "recover", "backup", "restore", "verify-backup"])
    parser.add_argument("--username")
    parser.add_argument("--source")
    parser.add_argument("--destination")
    args = parser.parse_args()
    os.umask(0o077)
    if args.command in ("backup", "restore", "verify-backup"):
        from scheduler.backup import backup_database, restore_database, verify
        if args.command != "backup" and not args.source:
            parser.error("--source is required")
        if args.command != "verify-backup" and not args.destination:
            parser.error("--destination is required; existing files are never overwritten")
        if args.command == "backup":
            from scheduler.db import audit
            db = Database()
            try:
                backup_database(db.path, args.destination)
            except Exception:
                with db.transaction() as c:
                    audit(c, "backup.failed", "database")
                raise
            else:
                with db.transaction() as c:
                    audit(c, "backup.completed", "database")
            finally:
                db.engine.dispose()
        elif args.command == "restore":
            restore_database(args.source, args.destination, os.environ.get("TOTP_KEY_FILE", "/keys/totp.key"))
        else:
            verify(args.source)
        print("Database verified." + (" Restore quarantined: all sessions and public links revoked; collection closed." if args.command == "restore" else ""))
        return
    if args.command == "init-key":
        path = Path(os.environ.get("TOTP_KEY_FILE", "/keys/totp.key"))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as file:
                file.write(Fernet.generate_key())
        except FileExistsError:
            parser.exit(1, "Key already exists; refusing to overwrite authentication key.\n")
        path.chmod(0o600)
        print("Encryption key initialized; protect and back up this file separately.")
        return
    if args.command == "migrate":
        migrate()
        print("Migration complete; any existing database was backed up first.")
        return
    if not args.username:
        parser.error("--username is required")
    db = Database()
    db.check()
    security = Security(os.environ.get("TOTP_KEY_FILE", "/keys/totp.key"))
    auth = Auth(db, security)
    password = getpass.getpass("New password (15–256 characters): ")
    if password != getpass.getpass("Repeat password: "):
        parser.error("Passwords differ")
    try:
        if args.command == "bootstrap":
            auth.bootstrap(args.username, password)
        else:
            auth.local_recovery(args.username, password)
    except Problem as exc:
        parser.exit(1, exc.message + "\n")
    print("Account ready. Sign in over HTTPS and enroll TOTP before staff access.")


if __name__ == "__main__":
    main()
