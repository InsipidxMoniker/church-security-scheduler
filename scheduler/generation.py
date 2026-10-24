"""Snapshot -> isolated solve -> independently validate -> atomic compare/commit."""
import asyncio
import json
import logging
import math
import os
import sys
import threading
import time
import uuid
from importlib.metadata import version

from starlette.concurrency import run_in_threadpool

from scheduler.db import audit, row
from scheduler.schedule_validation import (SLOT_KEYS, diagnostics, objective_values,
                                           validate_assignments, validate_snapshot)
from scheduler.security import Problem

logger = logging.getLogger(__name__)
# Deployment contract: one Uvicorn process. Shared across service instances in it.
GENERATION_GATE = threading.Lock()


class SolverProcess:
    def __init__(self, budget=10.0):
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("Solver budget must be positive and finite")
        self.budget = budget

    async def run(self, snapshot):
        started = time.monotonic()
        process = None

        def failure(status, message):
            return {"assignments": None, "metadata": {
                "status": status, "stages": [], "completed_stages": [], "last_completed_stage": None,
                "objectives": None, "canonicalization_complete": False, "canonical_slots": 0,
                "elapsed_seconds": time.monotonic() - started, "solver_version": version("ortools"),
                "parameters": {"num_search_workers": 1, "random_seed": 0, "log_search_progress": False},
                "budget_seconds": self.budget, "tie_break_algorithm": "church-scheduler-date-tiebreak-v1"},
                "problems": [message]}
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "scheduler.solver_worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # Do not pass database/key paths or security configuration.
                env={k: v for k, v in os.environ.items() if k in
                     ("PATH", "SYSTEMROOT", "WINDIR", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED")},
            )
            # Three seconds cover interpreter/import/IPC startup only. The worker
            # independently shares ONE budget across construction and all solves.
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(
                {"snapshot": snapshot, "budget": self.budget}).encode()), self.budget + 3)
            if process.returncode != 0:
                raise ValueError("Worker exit")
            result = json.loads(output)
            if (not isinstance(result, dict) or not isinstance(result.get("metadata"), dict)
                or result["metadata"].get("status") not in
                ("OPTIMAL", "FEASIBLE", "LOCK_CONFLICT", "UNKNOWN/TIMEOUT", "MODEL_INVALID", "INFEASIBLE", "WORKER_FAILURE")):
                raise ValueError("Worker protocol")
            if result["metadata"]["status"] == "WORKER_FAILURE":
                result = failure("WORKER_FAILURE", "Solver worker failed; draft unchanged.")
            result["metadata"]["worker_elapsed_seconds"] = time.monotonic() - started
            return result
        except TimeoutError:
            return failure("UNKNOWN/TIMEOUT", "Solver worker exceeded its total lifecycle deadline; draft unchanged.")
        except (OSError, ValueError):
            return failure("WORKER_FAILURE", "Solver worker crashed or returned an invalid response; draft unchanged.")
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 0.5)
                except TimeoutError:
                    process.kill()
                    await process.wait()


def invalidate_slot_approvals(c, version_id, service, position, now):
    affected = [r[0] for r in c.exec_driver_sql(
        "SELECT id FROM vacancy_approvals WHERE version_id=? AND service_number=? AND position_number=? AND invalidated_at IS NULL "
        "UNION ALL SELECT id FROM rotation_approvals WHERE version_id=? AND invalidated_at IS NULL AND ((first_service=? AND first_position=?) OR (second_service=? AND second_position=?))",
        (version_id, service, position, version_id, service, position, service, position))]
    c.exec_driver_sql("UPDATE vacancy_approvals SET invalidated_at=? WHERE version_id=? AND service_number=? AND position_number=? AND invalidated_at IS NULL",
                      (now, version_id, service, position))
    c.exec_driver_sql("UPDATE rotation_approvals SET invalidated_at=? WHERE version_id=? AND invalidated_at IS NULL AND ((first_service=? AND first_position=?) OR (second_service=? AND second_position=?))",
                      (now, version_id, service, position, service, position))
    for approval_id in affected:
        audit(c, "approval.invalidated", approval_id, details={"version_id": version_id, "slot": [service, position], "cause": "slot contents or classification changed"})


def invalidate_ineligible_approvals(c, person_id, now):
    """Eligibility changes permanently invalidate affected exact pairs, never slots."""
    invalid = [r[0] for r in c.exec_driver_sql(
        "SELECT r.id FROM rotation_approvals r JOIN schedule_versions v ON v.id=r.version_id "
        "JOIN volunteers p ON p.id=r.volunteer_id LEFT JOIN availability a ON a.date_id=r.date_id AND a.volunteer_id=p.id "
        "WHERE v.status='draft' AND r.volunteer_id=? AND r.invalidated_at IS NULL AND "
        "(p.active=0 OR coalesce(a.service_1_available,0)=0 OR coalesce(a.service_2_available,0)=0 OR "
        "(p.restricted_qualified=0 AND (r.first_position IN (1,2) OR r.second_position IN (1,2))))", (person_id,))]
    for approval_id in invalid:
        c.exec_driver_sql("UPDATE rotation_approvals SET invalidated_at=? WHERE id=?", (now, approval_id))
        audit(c, "approval.invalidated", approval_id, details={"cause": "live eligibility changed", "volunteer_id": person_id})


class Generation:
    def __init__(self, db, flow, runner=None):
        self.db, self.flow = db, flow
        self.runner = runner or SolverProcess()

    def capture(self, c, date_id):
        day = self.flow.date_row(c, date_id)
        settings = row(c, "SELECT config_epoch,roster_epoch,service_1_label,service_2_label,service_1_time,service_2_time,essentiality_confirmed FROM settings WHERE id=1")
        draft = row(c, "SELECT * FROM schedule_versions WHERE date_id=? AND status='draft'", (date_id,))
        if draft:
            config = json.loads(draft["config_snapshot"])
            slots = [dict(r) for r in c.exec_driver_sql("SELECT service_number,position_number,volunteer_id,slot_revision,locked,zone,essential FROM slots WHERE version_id=? ORDER BY service_number,position_number", (draft["id"],)).mappings()]
        else:
            config = {k: settings[k] for k in ("service_1_label", "service_2_label", "service_1_time", "service_2_time")}
            config["timezone"] = day["timezone_snapshot"]
            config["positions"] = [dict(r) for r in c.exec_driver_sql("SELECT * FROM positions ORDER BY number").mappings()]
            slots = [{"service_number": s, "position_number": p, "volunteer_id": None, "slot_revision": 0,
                      "locked": False, "zone": config["positions"][p-1]["zone"], "essential": bool(config["positions"][p-1]["essential"])} for s, p in SLOT_KEYS]
        volunteers = [dict(r) for r in c.exec_driver_sql(
            "SELECT v.id,v.active,v.restricted_qualified,a.service_1_available,a.service_2_available FROM volunteers v "
            "LEFT JOIN availability a ON a.volunteer_id=v.id AND a.date_id=? ORDER BY v.id", (date_id,)).mappings()]
        for v in volunteers:
            for field in ("active", "restricted_qualified", "service_1_available", "service_2_available"):
                if v[field] is not None:
                    v[field] = bool(v[field])
        expected = {"draft_id": draft["id"] if draft else "", "draft_revision": draft["draft_revision"] if draft else 0,
                    "date_input_epoch": day["date_input_epoch"], "roster_epoch": settings["roster_epoch"], "config_epoch": settings["config_epoch"]}
        snapshot = {"date_id": date_id, "local_date": day["local_date"], "config": config,
                    "input_epochs": expected, "slots": slots, "volunteers": volunteers}
        return day, settings, draft, snapshot

    def review(self, raw, date_id):
        with self.db.transaction() as c:
            self.flow.staff(c, raw)
            day, settings, draft, snapshot = self.capture(c, date_id)
            names = {r["id"]: r["public_label"] for r in c.exec_driver_sql("SELECT id,public_label FROM volunteers").mappings()}
        try:
            validate_snapshot(snapshot)
            problems = validate_assignments(snapshot, snapshot["slots"]) if draft else []
            if snapshot["config"].get("timezone") != day["timezone_snapshot"]:
                problems.append("Draft timezone does not match the immutable Sunday timezone; repair is required.")
        except (ValueError, KeyError, TypeError):
            problems = ["Draft slot/configuration snapshot is malformed; staff repair is required."]
        stale = bool(draft and draft["config_epoch"] != settings["config_epoch"])
        if stale:
            problems.append("Draft configuration is stale. Review and explicitly refresh configuration on the draft page. Existing snapshots are preserved until refresh.")
        for s in snapshot["slots"]:
            s["public_label"] = names.get(s["volunteer_id"], "Unknown volunteer") if s["volunteer_id"] else None
        return {"day": day, "draft": {"id": draft["id"], "draft_revision": draft["draft_revision"]} if draft else None,
                "config": snapshot["config"], "slots": snapshot["slots"] if draft else [],
                "expected": snapshot["input_epochs"], "problems": problems, "stale_configuration": stale,
                "metadata": json.loads(draft["solver_metadata"]) if draft and draft["solver_metadata"] else None,
                "diagnostics": diagnostics(snapshot, snapshot["slots"]), "setup_confirmed": bool(settings["essentiality_confirmed"])}

    def prepare(self, raw, csrf, date_id, expected):
        with self.db.transaction() as c:
            self.flow.staff(c, raw, csrf)
            day, settings, draft, snapshot = self.capture(c, date_id)
            if snapshot["input_epochs"] != expected:
                raise Problem(409, "Inputs or draft changed; reload the draft review before generating")
            if day["first_published_at"] is not None and (not draft or draft["base_version_id"] != day["current_published_version_id"]):
                raise Problem(409, "Create an amendment draft from the current publication before generating")
            if draft and draft["config_epoch"] != settings["config_epoch"]:
                raise Problem(409, "Draft configuration is stale; review and explicitly refresh configuration on the draft page. No snapshots changed.")
            if not all(snapshot["config"].get(f"service_{s}_label") for s in (1, 2)):
                raise Problem(409, "An Administrator must configure service labels before generation")
            try:
                validate_snapshot(snapshot)
                if snapshot["config"]["timezone"] != day["timezone_snapshot"]:
                    raise ValueError("Timezone mismatch")
            except (ValueError, KeyError, TypeError):
                raise Problem(409, "Invalid draft snapshot; repair is required before generation") from None
            conflicts = validate_assignments(snapshot, snapshot["slots"], locks_only=True)
            if conflicts:
                raise Problem(409, "LOCK_CONFLICT: " + " ".join(conflicts))
        return snapshot

    def commit(self, raw, csrf, snapshot, result):
        assignments, metadata = result.get("assignments"), result["metadata"]
        status = metadata.get("status")
        if status not in ("OPTIMAL", "FEASIBLE") or assignments is None:
            if status in ("MODEL_INVALID", "INFEASIBLE", "WORKER_FAILURE"):
                logger.error("Schedule generation internal failure: %s", status)
            # Failed attempts keep provenance in append-only audit, never replace
            # the existing draft's successful metadata or assignment rows.
            with self.db.transaction() as c:
                actor = self.flow.staff(c, raw, csrf)
                audit(c, "draft.generation_failed", snapshot["date_id"], actor=actor["id"],
                      details={"solver_metadata": metadata, "input_epochs": snapshot["input_epochs"],
                               "input_snapshot": snapshot, "problems": result.get("problems", [])})
            raise Problem(409 if status == "LOCK_CONFLICT" else 503,
                          f"{status}: " + " ".join(result.get("problems") or ["No usable solver result; draft unchanged."]))
        try:
            conflicts = validate_assignments(snapshot, assignments)
            values = objective_values(snapshot, assignments)
            if conflicts or values != metadata.get("objectives"):
                raise ValueError("Independent validation")
            if status == "OPTIMAL" and (metadata.get("completed_stages") != ["E", "O", "B", "U"] or not metadata.get("canonicalization_complete")):
                raise ValueError("False optimal status")
        except (TypeError, KeyError, ValueError):
            logger.error("Schedule generation independent validation failed")
            raise Problem(503, "Invalid solver result; draft unchanged") from None
        with self.db.transaction() as c:
            actor = self.flow.staff(c, raw, csrf)
            day, settings, draft, current = self.capture(c, snapshot["date_id"])
            if current != snapshot or (day["first_published_at"] is not None and (not draft or draft["base_version_id"] != day["current_published_version_id"])):
                raise Problem(409, "Inputs or draft changed during generation; newer state preserved. Reload and retry.")
            version_id = draft["id"] if draft else str(uuid.uuid4())
            now = self.flow.clock()
            metadata = {**metadata, "input_epochs": snapshot["input_epochs"], "input_snapshot": snapshot,
                        "diagnostics": diagnostics(snapshot, assignments)}
            if not draft:
                c.exec_driver_sql("INSERT INTO schedule_versions(id,date_id,status,config_snapshot,config_epoch,creator) VALUES(?,?,'draft',?,?,?)",
                                  (version_id, day["id"], json.dumps(snapshot["config"], sort_keys=True), settings["config_epoch"], actor["id"]))
            before = {(s["service_number"], s["position_number"]): s for s in snapshot["slots"]}
            changes = []
            for slot in assignments:
                s, p = slot["service_number"], slot["position_number"]
                old = before[s, p]
                if not draft:
                    c.exec_driver_sql("INSERT INTO slots(version_id,date_id,service_number,position_number,volunteer_id,source,zone,essential) VALUES(?,?,?,?,?,'solver',?,?)",
                                      (version_id, day["id"], s, p, slot["volunteer_id"], old["zone"], int(old["essential"])))
                elif slot["volunteer_id"] != old["volunteer_id"]:
                    changes.append((slot, old))
            # Clear all touched assignments first to permit valid simultaneous moves
            # under SQLite's immediate per-service unique constraint.
            for slot, old in changes:
                c.exec_driver_sql("UPDATE slots SET volunteer_id=NULL WHERE version_id=? AND service_number=? AND position_number=?",
                                  (version_id, slot["service_number"], slot["position_number"]))
            for slot, old in changes:
                s, p = slot["service_number"], slot["position_number"]
                invalidate_slot_approvals(c, version_id, s, p, now)
                c.exec_driver_sql("UPDATE slots SET volunteer_id=?,slot_revision=slot_revision+1,source='solver' WHERE version_id=? AND service_number=? AND position_number=?",
                                  (slot["volunteer_id"], version_id, s, p))
            revision = draft["draft_revision"] + 1 if draft else 1
            c.exec_driver_sql("UPDATE schedule_versions SET draft_revision=?,input_epochs=?,solver_metadata=?,updated_at=? WHERE id=?",
                              (revision, json.dumps(snapshot["input_epochs"], sort_keys=True), json.dumps(metadata, sort_keys=True), now, version_id))
            saved = [dict(r) for r in c.exec_driver_sql("SELECT service_number,position_number,volunteer_id FROM slots WHERE version_id=? ORDER BY service_number,position_number", (version_id,)).mappings()]
            if validate_assignments(snapshot, saved):
                raise Problem(503, "Persisted draft validation failed; transaction rolled back")
            audit(c, "draft.regenerated" if draft else "draft.generated", version_id, actor=actor["id"],
                  details={"date_id": day["id"], "draft_revision": revision, "input_epochs": snapshot["input_epochs"],
                           "status": status, "objectives": values, "changed_slots": [slot for slot, _ in changes]})
        return {"draft_id": version_id, "draft_revision": revision, "status": status, "metadata": metadata}

    async def generate(self, raw, csrf, date_id, expected):
        # Authorization precedes admission, including for an already busy worker.
        snapshot = await run_in_threadpool(self.prepare, raw, csrf, date_id, expected)
        if not GENERATION_GATE.acquire(blocking=False):
            raise Problem(429, "Another schedule generation is running; retry after it finishes", 1)
        try:
            result = await self.runner.run(snapshot)
            return await run_in_threadpool(self.commit, raw, csrf, snapshot, result)
        finally:
            GENERATION_GATE.release()
