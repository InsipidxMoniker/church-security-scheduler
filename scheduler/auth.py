import secrets
import os
import time
import uuid

from scheduler.db import audit, row
from scheduler.security import Problem, digest, password_policy, token, username_policy


class Auth:
    def __init__(self, db, security, clock=time.time):
        self.db, self.security, self.clock = db, security, clock
        self.account_limit = int(os.environ.get("AUTH_ACCOUNT_LIMIT", "5"))
        self.account_window = int(os.environ.get("AUTH_ACCOUNT_WINDOW", "900"))
        self.ip_limit = int(os.environ.get("AUTH_IP_LIMIT", "30"))
        self.ip_window = int(os.environ.get("AUTH_IP_WINDOW", "300"))
        if min(self.account_limit, self.account_window, self.ip_limit, self.ip_window) <= 0:
            raise RuntimeError("Authentication limits must be positive")

    def create_user(self, c, username, password_hash, role, actor=None):
        if role not in ("admin", "scheduler"):
            raise Problem(422, "Invalid role")
        user_id = str(uuid.uuid4())
        c.exec_driver_sql(
            "INSERT INTO admin_users(id,username,password_hash,role,totp_secret) VALUES(?,?,?,?,?)",
            (user_id, username_policy(username), password_hash, role, self.security.new_seed()),
        )
        audit(c, "staff.created", user_id, actor=actor, details={"role": role})
        return user_id

    def bootstrap(self, username, password):
        password_policy(password)
        encoded = self.security.hash_password(password)
        with self.db.transaction() as c:
            if c.exec_driver_sql("SELECT count(*) FROM admin_users").scalar():
                raise Problem(409, "Bootstrap already completed")
            return self.create_user(c, username, encoded, "admin")

    def revoke(self, c, user_id):
        c.exec_driver_sql("UPDATE staff_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (self.clock(), user_id))

    def change_access(self, c, actor, user_id, expected_epoch, role, active):
        if actor["role"] != "admin":
            raise Problem(403, "Administrator access required")
        target = row(c, "SELECT * FROM admin_users WHERE id=?", (user_id,))
        if not target:
            raise Problem(404, "Staff account not found")
        if target["credential_epoch"] != expected_epoch:
            raise Problem(409, "Account changed; refresh before retrying")
        if role not in ("admin", "scheduler"):
            raise Problem(422, "Invalid role")
        if not active and actor["id"] == user_id:
            raise Problem(409, "You cannot delete your signed-in account")
        if target["active"] and target["role"] == "admin" and (not active or role != "admin"):
            if c.exec_driver_sql("SELECT count(*) FROM admin_users WHERE active=1 AND role='admin'").scalar() <= 1:
                raise Problem(409, "The last active Administrator cannot be removed")
        c.exec_driver_sql("UPDATE admin_users SET role=?,active=?,credential_epoch=credential_epoch+1,updated_at=? WHERE id=?",
                          (role, int(active), self.clock(), user_id))
        self.revoke(c, user_id)
        audit(c, "staff.access_changed", user_id, actor=actor["id"], details={"before": {"role": target["role"], "active": target["active"]}, "after": {"role": role, "active": active}, "credential_epoch": expected_epoch + 1})

    def reset(self, c, user_id, password_hash, actor=None):
        if not row(c, "SELECT id FROM admin_users WHERE id=?", (user_id,)):
            raise Problem(404, "Staff account not found")
        c.exec_driver_sql(
            "UPDATE admin_users SET password_hash=?,totp_secret=?,enrolled=0,last_totp_step=-1,credential_epoch=credential_epoch+1,updated_at=? WHERE id=?",
            (password_hash, self.security.new_seed(), self.clock(), user_id),
        )
        c.exec_driver_sql("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
        self.revoke(c, user_id)
        audit(c, "staff.credentials_reset", user_id, actor=actor)

    def local_recovery(self, username, password):
        password_policy(password)
        encoded = self.security.hash_password(password)
        with self.db.transaction() as c:
            user = row(c, "SELECT * FROM admin_users WHERE username=?", (username_policy(username),))
            if not user:
                raise Problem(404, "Staff account not found")
            self.reset(c, user["id"], encoded)
            c.exec_driver_sql("UPDATE admin_users SET active=1 WHERE id=?", (user["id"],))
            c.exec_driver_sql("DELETE FROM auth_throttles WHERE scope_hash=?", (digest("account:" + user["username"]),))
            audit(c, "staff.local_recovery", user["id"])

    def reserve_attempt(self, username, ip):
        now = self.clock()
        account = digest("account:" + username)
        with self.db.transaction() as c:
            c.exec_driver_sql("DELETE FROM auth_throttles WHERE expires_at<?", (now,))
            for scope, window, maximum in [(account, self.account_window, self.account_limit), (digest("ip:" + ip), self.ip_window, self.ip_limit)]:
                state = row(c, "SELECT * FROM auth_throttles WHERE scope_hash=?", (scope,))
                if state and state["blocked_until"] > now:
                    raise Problem(429, "Too many authentication attempts; retry later", max(1, int(state["blocked_until"] - now)))
                if state and now - state["window_start"] < window:
                    if state["count"] >= maximum:
                        raise Problem(429, "Too many authentication attempts; retry later", max(1, int(window - (now - state["window_start"]))))
                    c.exec_driver_sql("UPDATE auth_throttles SET count=count+1 WHERE scope_hash=?", (scope,))
                else:
                    c.exec_driver_sql("INSERT INTO auth_throttles VALUES(?,?,1,0,?) ON CONFLICT(scope_hash) DO UPDATE SET window_start=excluded.window_start,count=1,blocked_until=0,expires_at=excluded.expires_at", (scope, now, now + window * 2))
        return account

    def finish_attempt(self, scope, success):
        with self.db.transaction() as c:
            if success:
                c.exec_driver_sql("UPDATE auth_throttles SET count=max(0,count-1) WHERE scope_hash=?", (scope,))
            else:
                c.exec_driver_sql("UPDATE auth_throttles SET blocked_until=CASE WHEN count>=? THEN ? ELSE blocked_until END,expires_at=? WHERE scope_hash=?", (self.account_limit, self.clock() + self.account_window, self.clock() + self.account_window * 2, scope))

    def issue(self, c, user, kind):
        raw, csrf, now = token(), token(), self.clock()
        c.exec_driver_sql("DELETE FROM staff_sessions WHERE expires_at<?", (now,))
        c.exec_driver_sql("INSERT INTO staff_sessions(token_hash,user_id,kind,credential_epoch,created_at,last_used_at,expires_at,csrf_hash) VALUES(?,?,?,?,?,?,?,?)",
                          (digest(raw), user["id"], kind, user["credential_epoch"], now, now, now + (600 if kind == "pending" else 28800), digest(csrf)))
        return raw, csrf

    def session(self, c, raw, kind="staff", csrf=None, role=None):
        session = row(c, "SELECT * FROM staff_sessions WHERE token_hash=?", (digest(raw or ""),))
        now = self.clock()
        if not session or session["kind"] != kind or session["revoked_at"] is not None or session["expires_at"] <= now or session["last_used_at"] + 1800 <= now:
            raise Problem(401, "Sign in required")
        user = row(c, "SELECT * FROM admin_users WHERE id=?", (session["user_id"],))
        if not user["active"] or user["credential_epoch"] != session["credential_epoch"] or (kind == "staff" and not user["enrolled"]):
            raise Problem(401, "Sign in required")
        if role and user["role"] != role:
            raise Problem(403, "Administrator access required")
        if csrf is not None and not secrets.compare_digest(session["csrf_hash"], digest(csrf)):
            raise Problem(403, "Invalid CSRF token")
        c.exec_driver_sql("UPDATE staff_sessions SET last_used_at=? WHERE token_hash=?", (now, session["token_hash"]))
        return user, session

    def login(self, username, password, ip):
        username = username_policy(username)
        scope = self.reserve_attempt(username, ip)
        success = False
        try:
            with self.db.engine.connect() as c:
                user = row(c, "SELECT * FROM admin_users WHERE username=?", (username,))
            verified = self.security.verify(user["password_hash"] if user else self.security.dummy_hash, password)
            if not verified or not user or not user["active"]:
                raise Problem(401, "Invalid credentials")
            rehash = self.security.hash_password(password) if self.security.hasher.check_needs_rehash(user["password_hash"]) else None
            with self.db.transaction() as c:
                current = row(c, "SELECT * FROM admin_users WHERE id=?", (user["id"],))
                if not current["active"] or current["credential_epoch"] != user["credential_epoch"]:
                    raise Problem(401, "Invalid credentials")
                if rehash:
                    c.exec_driver_sql("UPDATE admin_users SET password_hash=? WHERE id=?", (rehash, user["id"]))
                result = self.issue(c, current, "pending")
            success = True
            return result
        finally:
            self.finish_attempt(scope, success)

    def mfa(self, raw, csrf, code, recovery, ip):
        with self.db.transaction() as c:
            user, _ = self.session(c, raw, "pending", csrf)
        scope = self.reserve_attempt(user["username"], ip)
        success = False
        try:
            with self.db.transaction() as c:
                user, session = self.session(c, raw, "pending", csrf)
                codes = []
                now = self.clock()
                if recovery:
                    found = row(c, "SELECT * FROM recovery_codes WHERE user_id=? AND code_hash=? AND consumed_at IS NULL", (user["id"], digest(code)))
                    if not user["enrolled"] or not found:
                        raise Problem(401, "Invalid credentials")
                    c.exec_driver_sql("UPDATE recovery_codes SET consumed_at=? WHERE user_id=? AND code_hash=?", (now, user["id"], digest(code)))
                    self.revoke(c, user["id"])
                    audit(c, "staff.recovery_code_used", user["id"], actor=user["id"])
                else:
                    step = self.security.totp_step(user, code, now)
                    if step is None:
                        raise Problem(401, "Invalid credentials")
                    c.exec_driver_sql("UPDATE admin_users SET last_totp_step=?,enrolled=1,updated_at=? WHERE id=?", (step, now, user["id"]))
                    if not user["enrolled"]:
                        codes = [secrets.token_hex(16) for _ in range(10)]
                        for value in codes:
                            c.exec_driver_sql("INSERT INTO recovery_codes(user_id,code_hash) VALUES(?,?)", (user["id"], digest(value)))
                        audit(c, "staff.mfa_enrolled", user["id"], actor=user["id"])
                c.exec_driver_sql("UPDATE staff_sessions SET revoked_at=? WHERE token_hash=?", (now, session["token_hash"]))
                result = self.issue(c, user, "staff")
                audit(c, "staff.login", user["id"], actor=user["id"])
            success = True
            return *result, codes
        finally:
            self.finish_attempt(scope, success)
