"""Transactional configuration, roster, credentials and availability services."""
import csv
import io
import json
import math
import os
import re
import secrets
import unicodedata
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from cryptography.fernet import InvalidToken

from scheduler.db import audit, row
from scheduler.security import Problem, digest, token

PUBLIC_FIELDS = "id,display_name,public_label,active,restricted_qualified,revision,(pin_hash IS NOT NULL) AS has_pin"
CHOICES = {"first": (True, False), "second": (False, True), "both": (True, True), "neither": (False, False)}


def label(value, field="Display name", maximum=100):
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value):
        raise Problem(422, f"{field} cannot contain control characters")
    value = value.strip()
    if not 1 <= len(value) <= maximum or any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value):
        raise Problem(422, f"{field} must contain 1–{maximum} characters without control characters")
    return value


def page_bounds(page, page_size, query=""):
    if page < 1 or page > 1000000 or not 1 <= page_size <= 100 or len(query) > 100:
        raise Problem(422, "Invalid page or search; use 1–100 rows per page")
    search = query.strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + search + "%", (page - 1) * page_size


class VolunteerFlow:
    def __init__(self, db, security, auth):
        self.db, self.security, self.auth = db, security, auth
        self.clock = auth.clock
        self.timezones = available_timezones()
        self.csv_max_bytes = int(os.environ.get("CSV_MAX_BYTES", "500000"))
        if self.csv_max_bytes <= 0 or self.csv_max_bytes * 4 // 3 + 4096 > int(os.environ.get("MAX_BODY_BYTES", "1048576")):
            raise RuntimeError("MAX_BODY_BYTES must accommodate the CSV limit and its encrypted preview")
        self.pin_account_limit = int(os.environ.get("PIN_ACCOUNT_LIMIT", "5"))
        self.pin_account_window = int(os.environ.get("PIN_ACCOUNT_WINDOW", "900"))
        self.pin_ip_limit = int(os.environ.get("PIN_IP_LIMIT", "30"))
        self.pin_ip_window = int(os.environ.get("PIN_IP_WINDOW", "300"))
        self.pin_global_limit = int(os.environ.get("PIN_GLOBAL_LIMIT", "120"))
        self.roster_ip_limit = int(os.environ.get("ROSTER_IP_LIMIT", "60"))
        if min(self.pin_account_limit, self.pin_account_window, self.pin_ip_limit, self.pin_ip_window, self.pin_global_limit, self.roster_ip_limit) <= 0:
            raise RuntimeError("Volunteer authentication limits must be positive")

    def staff(self, c, raw, csrf=None, admin=False):
        return self.auth.session(c, raw, csrf=csrf, role="admin" if admin else None)[0]

    def settings(self, c):
        result = row(c, "SELECT * FROM settings WHERE id=1")
        result["positions"] = [dict(r) for r in c.exec_driver_sql("SELECT * FROM positions ORDER BY number").mappings()]
        result["security_settings"] = json.loads(result["security_settings"])
        return result

    def read_settings(self, raw):
        with self.db.transaction() as c:
            self.staff(c, raw, admin=True)
            return self.settings(c)

    def save_settings(self, raw, data):
        with self.db.transaction() as c:
            actor = self.staff(c, raw, data.csrf, True)
            before = self.settings(c)
            if data.expected_revision != before["settings_revision"]:
                raise Problem(409, "Settings changed; reload and review the current configuration")
            try:
                if data.timezone not in self.timezones:
                    raise ValueError
                ZoneInfo(data.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise Problem(422, "Choose a valid IANA timezone, such as America/Los_Angeles") from None
            labels = [label(data.service_1_label, "First service label"), label(data.service_2_label, "Second service label")]
            times = [data.service_1_time or None, data.service_2_time or None]
            if any(t is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in times):
                raise Problem(422, "Service times must be HH:MM or left blank")
            if not math.isfinite(data.link_days) or data.link_days <= 0 or not math.isfinite(self.clock() + data.link_days * 86400):
                raise Problem(422, "Link lifespan must be a positive number of days")
            try:
                datetime.fromtimestamp(self.clock() + data.link_days * 86400, timezone.utc)
            except (OverflowError, OSError, ValueError):
                raise Problem(422, "Link lifespan exceeds the supported calendar range") from None
            essential = [getattr(data, f"essential_{i}") for i in range(1, 9)]
            config_changed = labels != [before["service_1_label"], before["service_2_label"]] or times != [before["service_1_time"], before["service_2_time"]] or essential != [bool(p["essential"]) for p in before["positions"]]
            c.exec_driver_sql("UPDATE settings SET timezone=?,service_1_label=?,service_2_label=?,service_1_time=?,service_2_time=?,essentiality_confirmed=?,settings_revision=settings_revision+1,config_epoch=config_epoch+?,security_settings=?,updated_at=? WHERE id=1",
                              (data.timezone, *labels, *times, int(data.essentiality_confirmed), int(config_changed), json.dumps({**before["security_settings"], "pin_digits": data.pin_digits, "link_days": data.link_days}), self.clock()))
            for number, value in enumerate(essential, 1):
                c.exec_driver_sql("UPDATE positions SET essential=? WHERE number=?", (int(value), number))
            after = self.settings(c)
            audit(c, "settings.updated", 1, actor=actor["id"], details={"before": before, "after": after})
            return after

    def roster(self, raw, page=1, page_size=50, query="", status="active"):
        search, offset = page_bounds(page, page_size, query)
        if status not in ("all", "active", "inactive"):
            raise Problem(422, "Invalid roster status")
        with self.db.transaction() as c:
            actor = self.staff(c, raw)
            where = "search_text LIKE ? ESCAPE '\\'" + (" AND active=?" if status != "all" else "")
            args = (search,) + ((int(status == "active"),) if status != "all" else ())
            total = c.exec_driver_sql("SELECT count(*) FROM volunteers WHERE " + where, args).scalar()
            rows = [dict(r) for r in c.exec_driver_sql("SELECT " + PUBLIC_FIELDS + " FROM volunteers WHERE " + where + " ORDER BY normalized_label,id LIMIT ? OFFSET ?", (*args, page_size, offset)).mappings()]
            return {"volunteers": rows, "total": total, "page": page, "page_size": page_size, "query": query, "status": status, "role": actor["role"]}

    def person(self, c, volunteer_id):
        result = row(c, "SELECT " + PUBLIC_FIELDS + " FROM volunteers WHERE id=?", (volunteer_id,))
        if not result:
            raise Problem(404, "Volunteer not found")
        return result

    def read_person(self, raw, volunteer_id):
        with self.db.transaction() as c:
            actor = self.staff(c, raw)
            return {"person": self.person(c, volunteer_id), "role": actor["role"]}

    def unique_label(self, c, normalized, active, volunteer_id=""):
        if active and row(c, "SELECT id FROM volunteers WHERE active=1 AND normalized_label=? AND id!=?", (normalized, volunteer_id)):
            raise Problem(409, "An active volunteer already uses this public label; add a distinguishing initial or label")

    def create_person(self, c, name, public_label, active, qualified):
        name = label(name)
        public_label = label(public_label or name, "Public label")
        self.unique_label(c, public_label.casefold(), active)
        volunteer_id = str(uuid.uuid4())
        c.exec_driver_sql("INSERT INTO volunteers(id,display_name,public_label,normalized_label,search_text,active,restricted_qualified) VALUES(?,?,?,?,?,?,?)",
                          (volunteer_id, name, public_label, public_label.casefold(), (name + " " + public_label).casefold(), int(active), int(qualified)))
        return volunteer_id

    def save_person(self, raw, data, volunteer_id=None, deactivate=False):
        with self.db.transaction() as c:
            actor = self.staff(c, raw, data.csrf)
            if volunteer_id is None:
                volunteer_id = self.create_person(c, data.display_name, data.public_label, data.active, data.restricted_qualified)
                before = None
            else:
                before = self.person(c, volunteer_id)
                if before["revision"] != data.expected_revision:
                    raise Problem(409, "Volunteer changed; reload the current roster entry")
                if deactivate:
                    if not data.confirmed:
                        raise Problem(422, "Explicit removal confirmation required")
                    # Keep the current identity and qualifications; reuse the edit path's
                    # epoch, session revocation, approval invalidation and audit handling.
                    data = SimpleNamespace(**{key: before[key] for key in ("display_name", "public_label", "restricted_qualified")}, active=False)
                name = label(data.display_name)
                public = label(data.public_label or name, "Public label")
                self.unique_label(c, public.casefold(), data.active, volunteer_id)
                if (name, public, data.active, data.restricted_qualified) == (before["display_name"], before["public_label"], bool(before["active"]), bool(before["restricted_qualified"])):
                    return before
                c.exec_driver_sql("UPDATE volunteers SET display_name=?,public_label=?,normalized_label=?,search_text=?,active=?,restricted_qualified=?,revision=revision+1,credential_epoch=credential_epoch+?,updated_at=? WHERE id=?",
                                  (name, public, public.casefold(), (name + " " + public).casefold(), int(data.active), int(data.restricted_qualified), int(bool(before["active"]) and not data.active), self.clock(), volunteer_id))
                if not data.active:
                    c.exec_driver_sql("UPDATE volunteer_sessions SET revoked_at=? WHERE volunteer_id=? AND revoked_at IS NULL", (self.clock(), volunteer_id))
            c.exec_driver_sql("UPDATE settings SET roster_epoch=roster_epoch+1 WHERE id=1")
            from scheduler.generation import invalidate_ineligible_approvals
            invalidate_ineligible_approvals(c, volunteer_id, self.clock())
            after = self.person(c, volunteer_id)
            audit(c, "roster.created" if before is None else "roster.updated", volunteer_id, actor=actor["id"], details={"before": before, "after": after})
            return after

    def csv_rows(self, c, text):
        try:
            encoded_size = len(text.encode("utf-8"))
        except UnicodeError:
            raise Problem(422, "CSV must be UTF-8 text") from None
        if encoded_size > self.csv_max_bytes:
            raise Problem(422, f"CSV is too large; use a smaller import batch (up to {self.csv_max_bytes} UTF-8 bytes)")
        errors, warnings, rows, seen = [], [], [], set()
        try:
            reader = csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
            if next(reader, None) != ["display_name", "restricted_qualified"]:
                return [], ["Header must be display_name,restricted_qualified"], []
            existing = {r["normalized_label"] for r in c.exec_driver_sql("SELECT normalized_label FROM volunteers WHERE active=1").mappings()}
            # Bounded input size, one batched query; no per-row database queries.
            names = {r["display_name"].casefold() for r in c.exec_driver_sql("SELECT display_name FROM volunteers").mappings()}
            for fields in reader:
                number = reader.line_num
                try:
                    if len(fields) != 2 or fields[1] not in ("true", "false"):
                        raise Problem(422, "Two columns required; qualification must be true or false")
                    name = label(fields[0])
                    normalized = name.casefold()
                    if normalized in seen or normalized in existing:
                        raise Problem(422, "Duplicate active public label; distinguish names in the CSV")
                    if normalized in names:
                        warnings.append(f"Row {number}: name exists in roster; creates a new identity, never merges")
                    seen.add(normalized)
                    rows.append({"display_name": name, "restricted_qualified": fields[1] == "true"})
                except Problem as exc:
                    errors.append(f"Row {number}: {exc.message}")
            if not rows and not errors:
                errors.append("CSV has no volunteer rows")
        except (csv.Error, UnicodeError):
            errors.append("Malformed UTF-8 CSV")
        return rows, errors, warnings

    def preview_csv(self, raw, csrf, text):
        with self.db.transaction() as c:
            actor = self.staff(c, raw, csrf)
            rows, errors, warnings = self.csv_rows(c, text)
            epoch = c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar()
        preview = self.security.cipher.encrypt(json.dumps({"actor": actor["id"], "text": text, "epoch": epoch, "purpose": "roster-import"}).encode()).decode() if not errors else None
        return {"rows": rows, "errors": errors, "warnings": warnings, "preview": preview}

    def commit_csv(self, raw, csrf, preview):
        try:
            saved = json.loads(self.security.cipher.decrypt(preview.encode(), ttl=900))
            if saved.get("purpose") != "roster-import":
                raise ValueError
        except (InvalidToken, ValueError, UnicodeError):
            raise Problem(409, "Import preview expired or changed; preview the CSV again") from None
        with self.db.transaction() as c:
            actor = self.staff(c, raw, csrf)
            if actor["id"] != saved["actor"] or c.exec_driver_sql("SELECT roster_epoch FROM settings").scalar() != saved["epoch"]:
                raise Problem(409, "Roster changed or preview belongs to another staff member; preview again")
            rows, errors, _ = self.csv_rows(c, saved["text"])
            if errors:
                raise Problem(409, "CSV no longer validates; preview again")
            ids = [self.create_person(c, r["display_name"], "", True, r["restricted_qualified"]) for r in rows]
            c.exec_driver_sql("UPDATE settings SET roster_epoch=roster_epoch+1 WHERE id=1")
            for volunteer_id in ids:
                audit(c, "roster.imported", volunteer_id, actor=actor["id"], details={"after": self.person(c, volunteer_id)})
            return {"created": len(ids)}

    def issue_pin(self, raw, csrf, volunteer_id, expected_revision, mode="random", custom_pin=""):
        with self.db.transaction() as c:
            self.staff(c, raw, csrf, True)
            before = self.person(c, volunteer_id)
            if before["revision"] != expected_revision:
                raise Problem(409, "Volunteer changed; reload before issuing a PIN")
            digits = self.settings(c)["security_settings"]["pin_digits"]
            settings_revision = c.exec_driver_sql("SELECT settings_revision FROM settings").scalar()
        if mode == "custom":
            if not re.fullmatch(r"[0-9]{4,12}", custom_pin):
                raise Problem(422, "Custom PIN must contain 4–12 digits (0–9); leading zeros are preserved")
            pin = custom_pin
        elif mode == "random" and not custom_pin:
            pin = "".join(secrets.choice("0123456789") for _ in range(digits))
        else:
            raise Problem(422, "Choose custom PIN to use the entered value, or leave it blank to generate a random PIN")
        encoded = self.security.hash_password(pin)
        encrypted = self.security.cipher.encrypt(json.dumps({"purpose": "volunteer-pin-display", "volunteer_id": volunteer_id, "pin": pin}).encode()).decode()
        with self.db.transaction() as c:
            actor = self.staff(c, raw, csrf, True)
            current = self.person(c, volunteer_id)
            if current["revision"] != expected_revision or c.exec_driver_sql("SELECT settings_revision FROM settings").scalar() != settings_revision:
                raise Problem(409, "Volunteer or PIN settings changed; reload and retry")
            c.exec_driver_sql("UPDATE volunteers SET pin_hash=?,credential_epoch=credential_epoch+1,revision=revision+1,updated_at=? WHERE id=?", (encoded, self.clock(), volunteer_id))
            c.exec_driver_sql("INSERT INTO volunteer_pin_secrets(volunteer_id,pin_ciphertext) VALUES(?,?) ON CONFLICT(volunteer_id) DO UPDATE SET pin_ciphertext=excluded.pin_ciphertext", (volunteer_id, encrypted))
            c.exec_driver_sql("UPDATE volunteer_sessions SET revoked_at=? WHERE volunteer_id=? AND revoked_at IS NULL", (self.clock(), volunteer_id))
            c.exec_driver_sql("DELETE FROM auth_throttles WHERE scope_hash=?", (digest("volunteer:" + volunteer_id),))
            audit(c, "volunteer.pin_reset" if before["has_pin"] else "volunteer.pin_issued", volunteer_id, actor=actor["id"], details={"revision": expected_revision + 1, "mode": mode})
        return {"pin": pin, "revision": expected_revision + 1, "public_label": before["public_label"]}

    def view_pin(self, raw, csrf, volunteer_id, expected_revision):
        with self.db.transaction() as c:
            self.staff(c, raw, csrf, True)
            person = self.person(c, volunteer_id)
            if person["revision"] != expected_revision:
                raise Problem(409, "Volunteer changed; reload before viewing the PIN")
            if not person["has_pin"]:
                raise Problem(409, "No PIN has been provisioned for this volunteer")
            stored = row(c, "SELECT pin_ciphertext FROM volunteer_pin_secrets WHERE volunteer_id=?", (volunteer_id,))
            if not stored:
                raise Problem(409, "This PIN has no saved display copy. Reset it once to enable Administrator viewing")
            encoded = c.exec_driver_sql("SELECT pin_hash FROM volunteers WHERE id=?", (volunteer_id,)).scalar()
        try:
            claim = json.loads(self.security.cipher.decrypt(stored["pin_ciphertext"].encode()))
            if not isinstance(claim, dict) or claim.get("purpose") != "volunteer-pin-display" or claim.get("volunteer_id") != volunteer_id or not isinstance(claim.get("pin"), str) or not re.fullmatch(r"[0-9]{4,12}", claim["pin"]):
                raise ValueError
            pin = claim["pin"]
        except (InvalidToken, ValueError, UnicodeError):
            raise Problem(409, "The saved PIN cannot be displayed. Reset it to replace the saved copy") from None
        # Verify the saved copy against the actual credential outside the write
        # transaction, using the same bounded hashing capacity as authentication.
        if not self.security.verify(encoded, pin):
            raise Problem(409, "The saved PIN does not match the current credential. Reset it to replace the saved copy")
        with self.db.transaction() as c:
            actor = self.staff(c, raw, csrf, True)
            current = self.person(c, volunteer_id)
            if current["revision"] != expected_revision or c.exec_driver_sql("SELECT pin_hash FROM volunteers WHERE id=?", (volunteer_id,)).scalar() != encoded:
                raise Problem(409, "Volunteer or PIN changed; reload before viewing the PIN")
            audit(c, "volunteer.pin_viewed", volunteer_id, actor=actor["id"], details={"revision": expected_revision})
        return {"pin": pin, "revision": expected_revision, "public_label": current["public_label"]}

    def clear_pin_block(self, raw, csrf, volunteer_id):
        with self.db.transaction() as c:
            actor = self.staff(c, raw, csrf, True)
            self.person(c, volunteer_id)
            c.exec_driver_sql("DELETE FROM auth_throttles WHERE scope_hash=?", (digest("volunteer:" + volunteer_id),))
            audit(c, "volunteer.block_cleared", volunteer_id, actor=actor["id"])
        return {"cleared": True}

    def dates(self, raw, page=1):
        _, offset = page_bounds(page, 50)
        with self.db.transaction() as c:
            self.staff(c, raw)
            return {"dates": [dict(r) for r in c.exec_driver_sql("SELECT * FROM service_dates ORDER BY local_date DESC LIMIT 50 OFFSET ?", (offset,)).mappings()], "page": page}

    def date_row(self, c, date_id):
        result = row(c, "SELECT * FROM service_dates WHERE id=?", (date_id,))
        if not result:
            raise Problem(404, "Sunday not found")
        return result

    def open_date(self, raw, csrf, local_date):
        try:
            day = date.fromisoformat(local_date)
            if day.isoformat() != local_date or day.weekday() != 6:
                raise ValueError
        except ValueError:
            raise Problem(422, "Choose a Sunday calendar date (YYYY-MM-DD)") from None
        with self.db.transaction() as c:
            actor = self.staff(c, raw, csrf)
            existing = row(c, "SELECT * FROM service_dates WHERE local_date=?", (local_date,))
            if existing:
                return existing
            settings = self.settings(c)
            if not settings["timezone"] or not settings["service_1_label"] or not settings["service_2_label"]:
                raise Problem(409, "An Administrator must configure the church timezone and service labels first")
            date_id = str(uuid.uuid4())
            c.exec_driver_sql("INSERT INTO service_dates(id,local_date,timezone_snapshot) VALUES(?,?,?)", (date_id, local_date, settings["timezone"]))
            audit(c, "date.opened", date_id, actor=actor["id"], details={"local_date": local_date, "timezone_snapshot": settings["timezone"], "date_input_epoch": 1})
            return self.date_row(c, date_id)

    def date_cas(self, c, date_id, expected_epoch):
        day = self.date_row(c, date_id)
        if day["date_input_epoch"] != expected_epoch:
            raise Problem(409, "Sunday inputs changed; reload before retrying")
        return day

    def collection(self, raw, csrf, date_id, expected_epoch, opened):
        with self.db.transaction() as c:
            actor = self.staff(c, raw, csrf)
            day = self.date_cas(c, date_id, expected_epoch)
            if day["first_published_at"] is not None and opened:
                raise Problem(409, "First publication permanently closed volunteer collection")
            if bool(day["availability_open"]) != opened:
                c.exec_driver_sql("UPDATE service_dates SET availability_open=?,date_input_epoch=date_input_epoch+1,updated_at=? WHERE id=?", (int(opened), self.clock(), date_id))
                audit(c, "date.collection_changed", date_id, actor=actor["id"], details={"open": opened, "date_input_epoch": expected_epoch + 1})
            return self.date_row(c, date_id)

    def manage_link(self, raw, csrf, date_id, expected_epoch, operation):
        with self.db.transaction() as c:
            actor = self.staff(c, raw, csrf)
            day = self.date_cas(c, date_id, expected_epoch)
            if day["first_published_at"] is not None:
                raise Problem(409, "Volunteer links cannot be issued after first publication")
            current = row(c, "SELECT * FROM date_links WHERE date_id=? AND revoked_at IS NULL", (date_id,))
            if operation == "create" and current:
                raise Problem(409, "A link was already issued; rotate it to replace a lost or expired link")
            c.exec_driver_sql("DELETE FROM date_link_secrets WHERE link_id IN (SELECT id FROM date_links WHERE date_id=?)", (date_id,))
            if current:
                c.exec_driver_sql("UPDATE date_links SET revoked_at=? WHERE id=?", (self.clock(), current["id"]))
                c.exec_driver_sql("UPDATE volunteer_sessions SET revoked_at=? WHERE link_id=? AND revoked_at IS NULL", (self.clock(), current["id"]))
            result = {}
            if operation != "revoke":
                settings = self.settings(c)
                generation = c.exec_driver_sql("SELECT coalesce(max(generation),0)+1 FROM date_links WHERE date_id=?", (date_id,)).scalar()
                raw_token, link_id, now = token(), str(uuid.uuid4()), self.clock()
                expires = now + settings["security_settings"]["link_days"] * 86400
                if not math.isfinite(expires):
                    raise Problem(422, "Configured link lifespan is too large")
                c.exec_driver_sql("INSERT INTO date_links(id,date_id,token_digest,generation,created_at,expires_at,creator) VALUES(?,?,?,?,?,?,?)", (link_id, date_id, digest(raw_token), generation, now, expires, actor["id"]))
                encrypted = self.security.cipher.encrypt(json.dumps({"purpose": "date-link-display", "link_id": link_id, "date_id": date_id, "generation": generation, "token": raw_token}).encode()).decode()
                c.exec_driver_sql("INSERT INTO date_link_secrets(link_id,token_ciphertext) VALUES(?,?)", (link_id, encrypted))
                result = {"token": raw_token, "expires_at": expires, "generation": generation}
            c.exec_driver_sql("UPDATE service_dates SET date_input_epoch=date_input_epoch+1,updated_at=? WHERE id=?", (self.clock(), date_id))
            audit(c, "date.link_" + operation, date_id, actor=actor["id"], details={"date_input_epoch": expected_epoch + 1})
            return {**result, "date_input_epoch": expected_epoch + 1}

    def rate(self, c, scope, window, limit):
        now, key = self.clock(), digest(scope)
        state = row(c, "SELECT * FROM auth_throttles WHERE scope_hash=?", (key,))
        if state and state["blocked_until"] > now:
            raise Problem(429, "Too many verification attempts; retry later", max(1, math.ceil(state["blocked_until"] - now)))
        if state and now - state["window_start"] < window:
            if state["count"] >= limit:
                raise Problem(429, "Too many requests; retry later", max(1, math.ceil(window - (now - state["window_start"]))))
            c.exec_driver_sql("UPDATE auth_throttles SET count=count+1 WHERE scope_hash=?", (key,))
        else:
            c.exec_driver_sql("INSERT INTO auth_throttles VALUES(?,?,1,0,?) ON CONFLICT(scope_hash) DO UPDATE SET window_start=excluded.window_start,count=1,blocked_until=0,expires_at=excluded.expires_at", (key, now, now + window * 2))
        return key, (state["window_start"] if state and now - state["window_start"] < window else now)

    def valid_link(self, c, raw_token):
        if not 20 <= len(raw_token) <= 128:
            raise Problem(404, "Link unavailable; contact your scheduler")
        link = row(c, "SELECT * FROM date_links WHERE token_digest=? AND revoked_at IS NULL AND expires_at>?", (digest(raw_token), self.clock()))
        if not link:
            raise Problem(404, "Link unavailable; contact your scheduler")
        day = self.date_row(c, link["date_id"])
        if not day["availability_open"] or day["first_published_at"] is not None:
            raise Problem(403, "Collection is closed; contact your scheduler")
        return link, day

    def public_roster(self, raw_token, ip, page=1, page_size=50, query=""):
        search, offset = page_bounds(page, page_size, query)
        with self.db.transaction() as c:
            c.exec_driver_sql("DELETE FROM auth_throttles WHERE expires_at<?", (self.clock(),))
            self.rate(c, "roster-ip:" + ip, 60, self.roster_ip_limit)
        with self.db.transaction() as c:
            link, day = self.valid_link(c, raw_token)
            settings = row(c, "SELECT service_1_label,service_2_label FROM settings WHERE id=1")
            people = [dict(r) for r in c.exec_driver_sql("SELECT id,public_label FROM volunteers WHERE active=1 AND normalized_label LIKE ? ESCAPE '\\' ORDER BY normalized_label,id LIMIT ? OFFSET ?", (search, page_size + 1, offset)).mappings()]
            csrf = self.security.cipher.encrypt(json.dumps({"purpose": "volunteer-verify", "link_id": link["id"], "nonce": token()}).encode()).decode()
            return {"local_date": day["local_date"], **settings, "volunteers": people[:page_size], "has_more": len(people) > page_size, "page": page, "page_size": page_size, "query": query, "csrf": csrf}

    def verify_pin(self, raw_token, csrf, csrf_cookie, volunteer_id, pin, ip):
        try:
            claim = json.loads(self.security.cipher.decrypt(csrf.encode(), ttl=600))
            if claim.get("purpose") != "volunteer-verify" or not secrets.compare_digest(digest(csrf), digest(csrf_cookie or "")):
                raise ValueError
        except (InvalidToken, ValueError, UnicodeError):
            raise Problem(403, "Refresh the availability link before verifying") from None
        with self.db.transaction() as c:
            link, _ = self.valid_link(c, raw_token)
            if link["id"] != claim["link_id"]:
                raise Problem(403, "Verification form belongs to another link; refresh")
            c.exec_driver_sql("DELETE FROM auth_throttles WHERE expires_at<?", (self.clock(),))
            self.rate(c, "pin-global", 60, self.pin_global_limit)
            self.rate(c, "pin-ip:" + ip, self.pin_ip_window, self.pin_ip_limit)
            reservation = self.rate(c, "volunteer:" + volunteer_id, self.pin_account_window, self.pin_account_limit)
            person = row(c, "SELECT * FROM volunteers WHERE id=?", (volunteer_id,))
        success = False
        failed_verification = False
        try:
            encoded = person["pin_hash"] if person and person["active"] and person["pin_hash"] else self.security.dummy_hash
            valid = self.security.verify(encoded, pin)
            if not valid or not person or not person["active"] or not person["pin_hash"]:
                failed_verification = True
                raise Problem(401, "Identity or PIN could not be verified")
            rehash = self.security.hash_password(pin) if self.security.hasher.check_needs_rehash(encoded) else None
            with self.db.transaction() as c:
                link, _ = self.valid_link(c, raw_token)
                current = row(c, "SELECT * FROM volunteers WHERE id=?", (volunteer_id,))
                if link["id"] != claim["link_id"] or not current["active"] or current["credential_epoch"] != person["credential_epoch"]:
                    raise Problem(401, "Identity or PIN could not be verified")
                if rehash:
                    c.exec_driver_sql("UPDATE volunteers SET pin_hash=? WHERE id=?", (rehash, volunteer_id))
                session, session_csrf, now = token(), token(), self.clock()
                c.exec_driver_sql("DELETE FROM volunteer_sessions WHERE expires_at<?", (now,))
                c.exec_driver_sql("INSERT INTO volunteer_sessions(token_hash,volunteer_id,date_id,link_id,link_generation,credential_epoch,created_at,last_used_at,expires_at,csrf_hash) VALUES(?,?,?,?,?,?,?,?,?,?)", (digest(session), volunteer_id, link["date_id"], link["id"], link["generation"], current["credential_epoch"], now, now, now + 7200, digest(session_csrf)))
                audit(c, "volunteer.verified", volunteer_id, volunteer=volunteer_id, details={"date_id": link["date_id"]})
                key, start = reservation
                c.exec_driver_sql("UPDATE auth_throttles SET count=max(0,count-1) WHERE scope_hash=? AND window_start=?", (key, start))
            success = True
            return session, session_csrf
        finally:
            if not success:
                key, start = reservation
                with self.db.transaction() as c:
                    if failed_verification:
                        c.exec_driver_sql("UPDATE auth_throttles SET blocked_until=CASE WHEN count>=? THEN ? ELSE blocked_until END,expires_at=? WHERE scope_hash=? AND window_start=?", (self.pin_account_limit, self.clock() + self.pin_account_window, self.clock() + self.pin_account_window * 2, key, start))
                    else:
                        c.exec_driver_sql("UPDATE auth_throttles SET count=max(0,count-1) WHERE scope_hash=? AND window_start=?", (key, start))

    def volunteer_session(self, c, raw_token, raw_session, csrf=None):
        link, day = self.valid_link(c, raw_token)
        session = row(c, "SELECT * FROM volunteer_sessions WHERE token_hash=?", (digest(raw_session or ""),))
        now = self.clock()
        if not session or session["revoked_at"] is not None or session["expires_at"] <= now or session["last_used_at"] + 1800 <= now or session["link_id"] != link["id"] or session["link_generation"] != link["generation"] or session["date_id"] != day["id"]:
            raise Problem(401, "Verify your identity and PIN for this Sunday")
        person = row(c, "SELECT * FROM volunteers WHERE id=?", (session["volunteer_id"],))
        if not person["active"] or person["credential_epoch"] != session["credential_epoch"]:
            raise Problem(401, "Verify your identity and PIN for this Sunday")
        if csrf is not None and not secrets.compare_digest(session["csrf_hash"], digest(csrf)):
            raise Problem(403, "Invalid CSRF token; reload your response")
        c.exec_driver_sql("UPDATE volunteer_sessions SET last_used_at=? WHERE token_hash=?", (now, session["token_hash"]))
        return person, day

    def response(self, c, date_id, volunteer_id):
        result = row(c, "SELECT service_1_available,service_2_available,revision FROM availability WHERE date_id=? AND volunteer_id=?", (date_id, volunteer_id))
        if result is None:
            return {"service_1_available": None, "service_2_available": None, "revision": 0, "choice": None}
        result["service_1_available"] = bool(result["service_1_available"])
        result["service_2_available"] = bool(result["service_2_available"])
        result["choice"] = next(k for k, v in CHOICES.items() if v == (bool(result["service_1_available"]), bool(result["service_2_available"])))
        return result

    def mine(self, raw_token, raw_session):
        with self.db.transaction() as c:
            person, day = self.volunteer_session(c, raw_token, raw_session)
            settings = row(c, "SELECT service_1_label,service_2_label FROM settings WHERE id=1")
            return {"local_date": day["local_date"], "public_label": person["public_label"], **settings, "response": self.response(c, day["id"], person["id"])}

    def write_response(self, c, day, person_id, expected_revision, first, second, actor=None, reason=None):
        current = self.response(c, day["id"], person_id)
        if current["revision"] != expected_revision:
            raise Problem(409, "Availability changed in another window; review the latest response before submitting again")
        revision = expected_revision + 1
        c.exec_driver_sql("INSERT INTO availability(date_id,volunteer_id,service_1_available,service_2_available,revision,source,actor_id,confirmation_reason,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(date_id,volunteer_id) DO UPDATE SET service_1_available=excluded.service_1_available,service_2_available=excluded.service_2_available,revision=excluded.revision,source=excluded.source,actor_id=excluded.actor_id,confirmation_reason=excluded.confirmation_reason,updated_at=excluded.updated_at",
                          (day["id"], person_id, int(first), int(second), revision, "admin" if actor else "volunteer", actor, reason, self.clock()))
        c.exec_driver_sql("UPDATE service_dates SET date_input_epoch=date_input_epoch+1,updated_at=? WHERE id=?", (self.clock(), day["id"]))
        from scheduler.generation import invalidate_ineligible_approvals
        invalidate_ineligible_approvals(c, person_id, self.clock())
        after = self.response(c, day["id"], person_id)
        audit(c, "availability.updated", day["id"], actor=actor, volunteer=None if actor else person_id, details={"volunteer_id": person_id, "before": current, "after": after, "confirmation_reason": reason, "date_input_epoch": day["date_input_epoch"] + 1})
        return after

    def save_mine(self, raw_token, raw_session, csrf, expected_revision, first, second, include_page=False):
        with self.db.transaction() as c:
            person, day = self.volunteer_session(c, raw_token, raw_session, csrf)
            response = self.write_response(c, day, person["id"], expected_revision, first, second)
            if include_page:
                settings = row(c, "SELECT service_1_label,service_2_label FROM settings WHERE id=1")
                return {"local_date": day["local_date"], "public_label": person["public_label"], **settings, "response": response}
            return response

    def staff_response(self, raw, csrf, date_id, volunteer_id, expected_revision, first, second, reason):
        with self.db.transaction() as c:
            actor = self.staff(c, raw, csrf)
            day = self.date_row(c, date_id)
            person = self.person(c, volunteer_id)
            if not person["active"]:
                raise Problem(422, "Reactivate the volunteer before recording a new response")
            if day["first_published_at"] is not None:
                draft = row(c, "SELECT base_version_id FROM schedule_versions WHERE date_id=? AND status='draft'", (date_id,))
                if not draft or not day["current_published_version_id"] or draft["base_version_id"] != day["current_published_version_id"]:
                    raise Problem(409, "Post-publication changes require an amendment draft from the current publication")
            reason = label(reason, "Volunteer confirmation reason", 1000)
            return self.write_response(c, day, volunteer_id, expected_revision, first, second, actor["id"], reason)

    def dashboard(self, raw, date_id, page=1):
        _, offset = page_bounds(page, 50)
        with self.db.transaction() as c:
            self.staff(c, raw)
            day = self.date_row(c, date_id)
            counts = row(c, "SELECT count(*) AS active_total,sum(a.volunteer_id IS NULL) AS no_response,coalesce(sum(a.service_1_available=1 AND a.service_2_available=0),0) AS first_only,coalesce(sum(a.service_1_available=0 AND a.service_2_available=1),0) AS second_only,coalesce(sum(a.service_1_available=1 AND a.service_2_available=1),0) AS both,coalesce(sum(a.service_1_available=0 AND a.service_2_available=0),0) AS neither,coalesce(sum(a.service_1_available),0) AS first_total,coalesce(sum(a.service_2_available),0) AS second_total,coalesce(sum(a.service_1_available=1 AND v.restricted_qualified=1),0) AS first_qualified,coalesce(sum(a.service_2_available=1 AND v.restricted_qualified=1),0) AS second_qualified FROM volunteers v LEFT JOIN availability a ON a.volunteer_id=v.id AND a.date_id=? WHERE v.active=1", (date_id,))
            counts["no_response"] = counts["no_response"] or 0
            people = [dict(r) for r in c.exec_driver_sql("SELECT v.id,v.public_label,v.restricted_qualified,a.service_1_available,a.service_2_available,coalesce(a.revision,0) AS response_revision FROM volunteers v LEFT JOIN availability a ON a.volunteer_id=v.id AND a.date_id=? WHERE v.active=1 ORDER BY v.normalized_label,v.id LIMIT 50 OFFSET ?", (date_id, offset)).mappings()]
            saved_link = row(c, "SELECT l.*,s.token_ciphertext FROM date_links l LEFT JOIN date_link_secrets s ON s.link_id=l.id WHERE l.date_id=? ORDER BY l.generation DESC LIMIT 1", (date_id,))
            link_token = None
            if saved_link and saved_link["revoked_at"] is None and saved_link["token_ciphertext"]:
                try:
                    claim = json.loads(self.security.cipher.decrypt(saved_link["token_ciphertext"].encode()))
                    if isinstance(claim, dict) and isinstance(claim.get("token"), str) and (claim.get("purpose"), claim.get("link_id"), claim.get("date_id"), claim.get("generation")) == ("date-link-display", saved_link["id"], date_id, saved_link["generation"]) and secrets.compare_digest(digest(claim["token"]), saved_link["token_digest"]):
                        link_token = claim["token"]
                except (InvalidToken, ValueError, UnicodeError, KeyError, TypeError):
                    # No token disclosure on corrupt/swapped ciphertext. Staff can
                    # still use the dashboard to rotate it; verification uses its digest.
                    pass
            link = {k: saved_link[k] for k in ("generation", "created_at", "expires_at", "revoked_at")} if saved_link else None
            labels = row(c, "SELECT service_1_label,service_2_label FROM settings WHERE id=1")
            return {"day": day, "counts": counts, "volunteers": people, "link": link, "link_token": link_token, "page": page, "now": self.clock(), **labels}
