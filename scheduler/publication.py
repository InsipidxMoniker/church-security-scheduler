"""Staff draft lifecycle and atomic publication; no solver business rules here."""
import copy
import hashlib
import json
import uuid

from scheduler.db import audit, row
from scheduler.generation import invalidate_slot_approvals
from scheduler.schedule_validation import diagnostics, validate_snapshot
from scheduler.security import Problem
from scheduler.volunteer_flow import label


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(slot):
    return sha(canonical({k: slot[k] for k in ("service_number", "position_number", "volunteer_id", "slot_revision", "zone", "essential")}))


def approvals(c, version_id, slots):
    indexed = {(s["service_number"], s["position_number"]): s for s in slots}
    vacancies, rotations = {}, {}
    for a in c.exec_driver_sql("SELECT * FROM vacancy_approvals WHERE version_id=? AND invalidated_at IS NULL", (version_id,)).mappings():
        s = indexed.get((a["service_number"], a["position_number"]))
        if s and s["essential"] and s["volunteer_id"] is None and a["slot_revision"] == s["slot_revision"] and a["fingerprint"] == fingerprint(s):
            vacancies[s["service_number"], s["position_number"]] = dict(a)
    for a in c.exec_driver_sql("SELECT * FROM rotation_approvals WHERE version_id=? AND invalidated_at IS NULL", (version_id,)).mappings():
        first, second = indexed.get((1, a["first_position"])), indexed.get((2, a["second_position"]))
        if first and second and first["volunteer_id"] == second["volunteer_id"] == a["volunteer_id"] and first["zone"] == second["zone"] and all(
            a[prefix + "_revision"] == s["slot_revision"] and a[prefix + "_fingerprint"] == fingerprint(s) for prefix, s in (("first", first), ("second", second))):
            rotations[a["volunteer_id"]] = dict(a)
    return vacancies, rotations


def conflicts(snapshot, slots, rotations):
    """Stable conflict identities permit strict subset comparison during repair."""
    people = {v["id"]: v for v in snapshot["volunteers"]}
    errors, by_person = set(), {}
    for s in slots:
        person_id = s["volunteer_id"]
        if person_id is None:
            continue
        key = (s["service_number"], s["position_number"])
        person = people.get(person_id)
        local = set()
        if not person or not person["active"]:
            local.add(("inactive", *key, person_id))
        if not person or person[f"service_{key[0]}_available"] is not True:
            local.add(("unavailable", *key, person_id))
        if key[1] in (1, 2) and (not person or not person["outside_available"]):
            local.add(("qualification", *key, person_id))
        errors.update(local)
        if local and s["locked"]:
            errors.add(("invalid_lock", *key, person_id))
        by_person.setdefault(person_id, []).append(s)
    for person_id, assigned in by_person.items():
        for i, first in enumerate(assigned):
            for second in assigned[i+1:]:
                pair = (person_id, first["service_number"], first["position_number"], second["service_number"], second["position_number"])
                if first["service_number"] == second["service_number"]:
                    errors.add(("duplicate", *pair))
                elif first["zone"] == second["zone"] and person_id not in rotations:
                    errors.add(("rotation_approval_required", *pair))
    return errors


def describe_conflict(error):
    kind = error[0]
    if kind in ("duplicate", "rotation_approval_required"):
        return (f"Volunteer {error[1]}: positions {error[2]}:{error[3]} and {error[4]}:{error[5]} "
                + ("conflict within one service; assign distinct volunteers." if kind == "duplicate" else "share the same zone; an exact-pair approval with a reason is required, or change an assignment."))
    return f"Service {error[1]}, position {error[2]}: " + {
        "inactive": "volunteer is inactive; clear or reassign this position.",
        "unavailable": "volunteer is unavailable or has no response; confirm availability or repair this position.",
        "qualification": "current Outside Available is required; clear or reassign this position.",
        "invalid_lock": "invalid locked assignment; explicitly unlock or repair before generation.",
    }[kind]


class Publication:
    def __init__(self, db, flow, generation):
        self.db, self.flow, self.generation = db, flow, generation

    def checked(self, c, raw, csrf, date_id, expected):
        actor = self.flow.staff(c, raw, csrf)
        day, settings, draft, snapshot = self.generation.capture(c, date_id)
        if snapshot["input_epochs"] != expected:
            raise Problem(409, "Draft or inputs changed; reload and review current state")
        if not draft:
            raise Problem(409, "No draft; create an amendment after publication")
        try:
            validate_snapshot(snapshot)
            if snapshot["config"]["timezone"] != day["timezone_snapshot"]:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise Problem(409, "Malformed draft snapshot; publication blocked") from None
        return actor, day, settings, draft, snapshot

    def review(self, raw, date_id):
        data = {}
        with self.db.transaction() as c:
            self.flow.staff(c, raw)
            day, settings, draft, snap = self.generation.capture(c, date_id)
            # Build one consistent review, including expected values and markers.
            data.update(day=day, expected=snap["input_epochs"], config=snap["config"], slots=snap["slots"] if draft else [],
                        draft={"id": draft["id"], "draft_revision": draft["draft_revision"]} if draft else None,
                        stale_configuration=bool(draft and draft["config_epoch"] != settings["config_epoch"]))
            names = {r["id"]: r["public_label"] for r in c.exec_driver_sql("SELECT id,public_label FROM volunteers").mappings()}
            data.update(metadata=json.loads(draft["solver_metadata"]) if draft and draft["solver_metadata"] else None,
                        diagnostics=diagnostics(snap, snap["slots"]), setup_confirmed=bool(settings["essentiality_confirmed"]))
            metadata = data["metadata"] or {}
            solved_epochs = metadata.get("input_epochs", {})
            data["optimization_current"] = bool(draft and metadata.get("objectives") is not None
                and all(k in metadata["objectives"] for k in ("RS", "RT", "RP1"))
                and all(solved_epochs.get(k) == snap["input_epochs"][k] for k in ("roster_epoch", "config_epoch", "date_input_epoch"))
                and solved_epochs.get("draft_revision", -1) + 1 == draft["draft_revision"])
            vac, rot = approvals(c, draft["id"], snap["slots"]) if draft else ({}, {})
            data["problems"] = [describe_conflict(e) for e in sorted(conflicts(snap, snap["slots"], rot))] if draft else []
            data["vacancy_approvals"], data["rotation_approvals"] = list(vac.values()), list(rot.values())
            data["publications"] = [dict(r) for r in c.exec_driver_sql("SELECT id,publication_number,published_at FROM schedule_versions WHERE date_id=? AND status='published' ORDER BY publication_number DESC", (date_id,)).mappings()]
            for s in data["slots"]:
                s.update(public_label=names.get(s["volunteer_id"]), vacancy_approved=(s["service_number"], s["position_number"]) in vac,
                         rotation_approved=s["volunteer_id"] in rot)
            data["idempotency_key"] = str(uuid.uuid4())
            data["current_config"] = {k: settings[k] for k in ("service_1_label", "service_2_label", "service_1_time", "service_2_time", "inside_zone_label", "outside_zone_label")}
            data["current_config"]["positions"] = [dict(r) for r in c.exec_driver_sql("SELECT * FROM positions ORDER BY number").mappings()]
        return data

    def edit(self, raw, csrf, date_id, expected, changes, rotation_reasons=None):
        with self.db.transaction() as c:
            actor, day, settings, draft, snap = self.checked(c, raw, csrf, date_id, expected)
            old = snap["slots"]
            proposed = copy.deepcopy(old)
            indexed = {(s["service_number"], s["position_number"]): s for s in proposed}
            if len({(x["service_number"], x["position_number"]) for x in changes}) != len(changes):
                raise Problem(422, "Each slot must be identified once")
            _, prior_rot = approvals(c, draft["id"], old)
            before = conflicts(snap, old, prior_rot)
            touched, content = [], []
            for change in changes:
                s = indexed.get((change["service_number"], change["position_number"]))
                if s is None:
                    raise Problem(422, "Invalid slot")
                if change["volunteer_id"] != s["volunteer_id"]:
                    s["volunteer_id"] = change["volunteer_id"]
                    s["slot_revision"] += 1
                    content.append(s)
                if s in content or bool(s["locked"]) != change["locked"]:
                    s["locked"] = change["locked"]
                    touched.append(s)
            if not touched and not rotation_reasons:
                return {"draft_id": draft["id"], "draft_revision": draft["draft_revision"]}
            now = self.flow.clock()
            for s in content:
                invalidate_slot_approvals(c, draft["id"], s["service_number"], s["position_number"], now)
            # Clear touched rows first so swaps satisfy immediate UNIQUE checks.
            for s in content:
                c.exec_driver_sql("UPDATE slots SET volunteer_id=NULL WHERE version_id=? AND service_number=? AND position_number=?", (draft["id"], s["service_number"], s["position_number"]))
            for s in touched:
                c.exec_driver_sql("UPDATE slots SET volunteer_id=?,slot_revision=?,locked=?,source='manual' WHERE version_id=? AND service_number=? AND position_number=?",
                                  (s["volunteer_id"], s["slot_revision"], int(s["locked"]), draft["id"], s["service_number"], s["position_number"]))
            for person_id, reason in (rotation_reasons or {}).items():
                pair = [s for s in proposed if s["volunteer_id"] == person_id]
                if len(pair) != 2 or [s["service_number"] for s in pair] != [1, 2] or pair[0]["zone"] != pair[1]["zone"]:
                    raise Problem(422, "Identify an exact same-zone pair for approval")
                if any(e[0] != "rotation_approval_required" for e in conflicts(snap, pair, {})):
                    raise Problem(409, "A rotation exception cannot approve unavailable, inactive or unqualified assignments")
                reason = label(reason, "Rotation approval reason", 1000)
                for previous in c.exec_driver_sql("SELECT id FROM rotation_approvals WHERE version_id=? AND volunteer_id=? AND invalidated_at IS NULL", (draft["id"], person_id)).scalars().all():
                    audit(c, "approval.invalidated", previous, actor=actor["id"], details={"cause": "explicit replacement"})
                c.exec_driver_sql("UPDATE rotation_approvals SET invalidated_at=? WHERE version_id=? AND volunteer_id=? AND invalidated_at IS NULL", (now, draft["id"], person_id))
                a, b = pair
                approval_id = str(uuid.uuid4())
                c.exec_driver_sql("INSERT INTO rotation_approvals(id,version_id,date_id,volunteer_id,first_position,second_position,first_revision,second_revision,first_fingerprint,second_fingerprint,reason,approving_user,approved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (approval_id, draft["id"], date_id, person_id, a["position_number"], b["position_number"], a["slot_revision"], b["slot_revision"], fingerprint(a), fingerprint(b), reason, actor["id"], now))
                audit(c, "rotation.approved", approval_id, actor=actor["id"], details={"version_id": draft["id"], "volunteer_id": person_id, "reason": reason})
            _, rot = approvals(c, draft["id"], proposed)
            after = conflicts(snap, proposed, rot)
            if after and (not before or not after < before):
                raise Problem(409, "Edit must resolve existing conflicts without new conflicts: " + " ".join(describe_conflict(e) for e in sorted(after)))
            if before and not after < before:
                raise Problem(409, "Invalid draft permits only conflict-reducing repairs")
            c.exec_driver_sql("UPDATE schedule_versions SET draft_revision=draft_revision+1,updated_at=? WHERE id=?", (now, draft["id"]))
            audit(c, "draft.edited", draft["id"], actor=actor["id"], details={"before": old, "after": proposed, "expected": expected})
            return {"draft_id": draft["id"], "draft_revision": draft["draft_revision"] + 1}

    def approve_vacancies(self, raw, csrf, date_id, expected, keys, reason):
        reason = label(reason, "Vacancy approval reason", 1000)
        with self.db.transaction() as c:
            actor, day, settings, draft, snap = self.checked(c, raw, csrf, date_id, expected)
            slots = {(s["service_number"], s["position_number"]): s for s in snap["slots"]}
            if not keys or len(set(keys)) != len(keys):
                raise Problem(422, "Select distinct identified vacancies")
            now = self.flow.clock()
            ids = []
            for key in keys:
                s = slots.get(key)
                if not s or not s["essential"] or s["volunteer_id"] is not None:
                    raise Problem(409, "Only currently empty essential slots can be approved")
                for previous in c.exec_driver_sql("SELECT id FROM vacancy_approvals WHERE version_id=? AND service_number=? AND position_number=? AND invalidated_at IS NULL", (draft["id"], *key)).scalars().all():
                    audit(c, "approval.invalidated", previous, actor=actor["id"], details={"cause": "explicit replacement"})
                c.exec_driver_sql("UPDATE vacancy_approvals SET invalidated_at=? WHERE version_id=? AND service_number=? AND position_number=? AND invalidated_at IS NULL", (now, draft["id"], *key))
                approval_id = str(uuid.uuid4())
                c.exec_driver_sql("INSERT INTO vacancy_approvals(id,version_id,date_id,service_number,position_number,slot_revision,fingerprint,reason,approving_user,approved_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                  (approval_id, draft["id"], date_id, *key, s["slot_revision"], fingerprint(s), reason, actor["id"], now))
                ids.append(approval_id)
                audit(c, "vacancy.approved", approval_id, actor=actor["id"], details={"version_id": draft["id"], "slot": key, "slot_revision": s["slot_revision"], "reason": reason})
            c.exec_driver_sql("UPDATE schedule_versions SET draft_revision=draft_revision+1,updated_at=? WHERE id=?", (now, draft["id"]))
            return {"approval_ids": ids}

    def refresh(self, raw, csrf, date_id, expected):
        with self.db.transaction() as c:
            actor, day, settings, draft, snap = self.checked(c, raw, csrf, date_id, expected)
            config = {k: settings[k] for k in ("service_1_label", "service_2_label", "service_1_time", "service_2_time", "inside_zone_label", "outside_zone_label")}
            config.update(timezone=day["timezone_snapshot"], positions=[dict(r) for r in c.exec_driver_sql("SELECT * FROM positions ORDER BY number").mappings()])
            now = self.flow.clock()
            for s in snap["slots"]:
                p = config["positions"][s["position_number"]-1]
                if (s["zone"], s["essential"]) != (p["zone"], p["essential"]):
                    invalidate_slot_approvals(c, draft["id"], s["service_number"], s["position_number"], now)
                    c.exec_driver_sql("UPDATE slots SET zone=?,essential=?,slot_revision=slot_revision+1 WHERE version_id=? AND service_number=? AND position_number=?", (p["zone"], p["essential"], draft["id"], s["service_number"], s["position_number"]))
            c.exec_driver_sql("UPDATE schedule_versions SET config_snapshot=?,config_epoch=?,draft_revision=draft_revision+1,updated_at=? WHERE id=?", (canonical(config), settings["config_epoch"], now, draft["id"]))
            audit(c, "draft.configuration_refreshed", draft["id"], actor=actor["id"], details={"before": snap["config"], "after": config})
            return {"refreshed": True}

    def publish(self, raw, csrf, date_id, expected, key, confirmed):
        if not confirmed:
            raise Problem(422, "Separate publication confirmation is required")
        if not isinstance(key, str) or not 1 <= len(key) <= 100:
            raise Problem(422, "A publication idempotency key is required")
        request_digest = sha(canonical({"expected": expected, "confirmed": confirmed}))
        with self.db.transaction() as c:
            actor = self.flow.staff(c, raw, csrf)
            prior = row(c, "SELECT * FROM idempotency WHERE actor_id=? AND date_id=? AND operation='publish' AND key=?", (actor["id"], date_id, key))
            if prior:
                if prior["request_digest"] != request_digest:
                    raise Problem(409, "Idempotency key was used for different publication content")
                return {"version_id": prior["version_id"], "replayed": True}
            actor, day, settings, draft, snap = self.checked(c, raw, csrf, date_id, expected)
            if not settings["essentiality_confirmed"] or not all(snap["config"].get(f"service_{s}_label") for s in (1, 2)):
                raise Problem(409, "Administrator must complete setup and confirm essential positions")
            if draft["config_epoch"] != settings["config_epoch"]:
                raise Problem(409, "Draft configuration is stale; review and refresh")
            if draft["base_version_id"] != day["current_published_version_id"]:
                raise Problem(409, "Draft base no longer matches the current publication")
            vac, rot = approvals(c, draft["id"], snap["slots"])
            errors = conflicts(snap, snap["slots"], rot)
            missing = [(s["service_number"], s["position_number"]) for s in snap["slots"] if s["essential"] and s["volunteer_id"] is None and (s["service_number"], s["position_number"]) not in vac]
            if errors or missing:
                raise Problem(409, "Publication blocked. " + " ".join(describe_conflict(e) for e in sorted(errors)) + "; essential vacancies requiring approval: " + str(missing))
            number = c.exec_driver_sql("SELECT coalesce(max(publication_number),0)+1 FROM schedule_versions WHERE date_id=?", (date_id,)).scalar()
            names = {r["id"]: r["public_label"] for r in c.exec_driver_sql("SELECT id,public_label FROM volunteers").mappings()}
            now = self.flow.clock()
            slots = [{**s, "display_name": names.get(s["volunteer_id"]), "vacancy_approved": (s["service_number"], s["position_number"]) in vac,
                      "rotation_approved": s["volunteer_id"] in rot} for s in snap["slots"]]
            payload = {"schema": 1, "version_id": draft["id"], "date_id": date_id, "local_date": day["local_date"], "timezone": day["timezone_snapshot"],
                       "publication_number": number, "base_version_id": draft["base_version_id"], "config": snap["config"], "slots": slots,
                       "vacancy_approvals": list(vac.values()), "rotation_approvals": list(rot.values()), "publisher": actor["id"], "published_at": now,
                       "input_epochs": expected, "solver_metadata": json.loads(draft["solver_metadata"]) if draft["solver_metadata"] else None}
            encoded = canonical(payload)
            c.exec_driver_sql("UPDATE schedule_versions SET status='published',publication_number=?,published_actor=?,published_at=?,published_payload=?,payload_hash=?,input_epochs=?,updated_at=? WHERE id=?",
                              (number, actor["id"], now, encoded, sha(encoded), canonical(expected), now, draft["id"]))
            c.exec_driver_sql("UPDATE service_dates SET current_published_version_id=?,first_published_at=coalesce(first_published_at,?),availability_open=0,date_input_epoch=date_input_epoch+1,updated_at=? WHERE id=?", (draft["id"], now, now, date_id))
            c.exec_driver_sql("UPDATE volunteer_sessions SET revoked_at=? WHERE date_id=? AND revoked_at IS NULL", (now, date_id))
            audit(c, "schedule.published", draft["id"], actor=actor["id"], details={"publication_number": number, "payload_hash": sha(encoded), "expected": expected})
            c.exec_driver_sql("INSERT INTO idempotency(actor_id,date_id,operation,key,request_digest,version_id,created_at) VALUES(?,?,'publish',?,?,?,?)", (actor["id"], date_id, key, request_digest, draft["id"], now))
            return {"version_id": draft["id"], "publication_number": number, "replayed": False}

    def amend(self, raw, csrf, date_id, base_id):
        with self.db.transaction() as c:
            actor = self.flow.staff(c, raw, csrf)
            day = self.flow.date_row(c, date_id)
            draft = row(c, "SELECT * FROM schedule_versions WHERE date_id=? AND status='draft'", (date_id,))
            if not base_id or base_id != day["current_published_version_id"]:
                raise Problem(409, "Amend only the latest publication; reload history")
            if draft:
                if draft["base_version_id"] != base_id:
                    raise Problem(409, "A different draft already exists")
                return {"draft_id": draft["id"], "replayed": True}
            base = row(c, "SELECT * FROM schedule_versions WHERE id=? AND date_id=? AND status='published'", (base_id, date_id))
            payload = self.decode(base)
            _, settings, _, snap = self.generation.capture(c, date_id)
            version_id = str(uuid.uuid4())
            c.exec_driver_sql("INSERT INTO schedule_versions(id,date_id,status,base_version_id,config_snapshot,config_epoch,creator) VALUES(?,?,'draft',?,?,?,?)", (version_id, date_id, base_id, canonical(snap["config"]), settings["config_epoch"], actor["id"]))
            for s in payload["slots"]:
                p = snap["config"]["positions"][s["position_number"]-1]
                c.exec_driver_sql("INSERT INTO slots(version_id,date_id,service_number,position_number,volunteer_id,locked,source,zone,essential) VALUES(?,?,?,?,?,?,'manual',?,?)", (version_id, date_id, s["service_number"], s["position_number"], s["volunteer_id"], int(s["locked"]), p["zone"], p["essential"]))
            c.exec_driver_sql("UPDATE service_dates SET date_input_epoch=date_input_epoch+1 WHERE id=?", (date_id,))
            audit(c, "schedule.amendment_created", version_id, actor=actor["id"], details={"base_version_id": base_id})
            return {"draft_id": version_id, "replayed": False}

    @staticmethod
    def decode(version):
        if not version or version["status"] != "published":
            raise Problem(404, "Published version not found")
        if sha(version["published_payload"]) != version["payload_hash"]:
            raise Problem(503, "Publication consistency check failed; contact an operator")
        return json.loads(version["published_payload"])

    def historical(self, raw, date_id, version_id):
        with self.db.transaction() as c:
            self.flow.staff(c, raw)
            day = self.flow.date_row(c, date_id)
            version = row(c, "SELECT * FROM schedule_versions WHERE date_id=? AND id=? AND status='published'", (date_id, version_id))
            payload = self.decode(version)
            return {"payload": payload, "current": day["current_published_version_id"] == version_id, "current_id": day["current_published_version_id"], "day": day}
