"""Track one outstanding print control per Moonraker printer in the reminder key/value store."""
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import bindparam, text

from core.audit import log_audit
from core.db_compat import as_utc_datetime
from core.errors import ErrorCode, OdinError
from core.system_config_values import config_text, write_config_text
from modules.printers.adapters.moonraker import control_confirmed
from modules.printers.support_levels import command_refusal, control_proof

log = logging.getLogger(__name__)

DEADLINES = {"pause": 30, "resume": 300, "stop": 300}
CONFIRMED = {"pause": "Print paused.", "resume": "Print resumed.", "stop": "Print stopped."}
PENDING = {
    "pause": "Pause sent. Waiting for the printer to confirm.",
    "resume": "Resume sent. The printer reheats before it continues; this can take a few minutes.",
    "stop": "Stop sent. The printer finishes unwinding filament before it reports stopped; this can take a few minutes.",
}


def _key(printer_id):
    return f"pending_control:{printer_id}"


def public_record(record):
    return {k: record[k] for k in ("action", "sent_at", "deadline_at")}


def _decode(db, key, value, *, cleanup=True, commit_cleanup=True):
    suffix = key.removeprefix("pending_control:")
    printer_id = int(suffix) if suffix.isascii() and suffix.isdigit() and len(suffix) <= 10 else None
    try:
        if printer_id is None or printer_id > 2147483647 or key != _key(printer_id):
            printer_id = None
            raise ValueError
        record = json.loads(config_text(value))
        if not isinstance(record, dict) or record["action"] not in DEADLINES:
            raise ValueError
        if not {"automatic", "job_id", "print_job_id", "print_attempt_uid",
                "actor_user_id", "actor_token_id", "awaiting_reply"} <= record.keys():
            raise ValueError
        if not all(type(record[k]) is bool for k in ("automatic", "awaiting_reply")):
            raise ValueError
        if any(record.get(k) is not None and type(record[k]) is not int
               for k in ("print_job_id", "job_id", "detection_id")):
            raise ValueError
        if any(datetime.fromisoformat(record[k]).tzinfo is None for k in ("sent_at", "deadline_at")):
            raise ValueError
        if "resume_paused_at" in record and datetime.fromisoformat(record["resume_paused_at"]).tzinfo is None:
            raise ValueError
        return record
    except (KeyError, TypeError, ValueError, RecursionError):
        if not cleanup:
            return None
        # Match the bytes we read so a concurrent replacement survives cleanup.
        removed = db.execute(text("DELETE FROM system_config WHERE key=:k AND CAST(value AS TEXT) IS NOT DISTINCT FROM :v"),
                             {"k": key, "v": value})
        if removed.rowcount:
            log_audit(db, "printer.invalid_pending_control", "printer", printer_id)
        if commit_cleanup:
            db.commit()
        return None


def _inside_deadline(record):
    return datetime.now(timezone.utc) < datetime.fromisoformat(record["deadline_at"])


def enrich_controls(db, printers):
    keys = {_key(p.id): p for p in printers if p.api_type == "moonraker"}
    for printer in keys.values():
        printer.pending_control = None
    if keys:
        rows = db.execute(text("SELECT key, CAST(value AS TEXT) FROM system_config WHERE key IN :keys")
                          .bindparams(bindparam("keys", expanding=True)), {"keys": list(keys)})
        for key, value in rows.all():
            record = _decode(db, key, value)
            if record and not record["awaiting_reply"] and _inside_deadline(record):
                keys[key].pending_control = public_record(record)
    return printers


def _read(db, printer_id, *, lock=False, cleanup=True):
    key = _key(printer_id)
    if lock:
        # Serialize even the first insertion. Release before any device I/O.
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT id FROM printers WHERE id=:id FOR UPDATE"), {"id": printer_id})
        else:
            db.execute(text("UPDATE system_config SET key=key WHERE key=:k"), {"k": key})
    row = db.execute(text("SELECT CAST(value AS TEXT) FROM system_config WHERE key=:k"), {"k": key}).first()
    return _decode(db, key, row[0], cleanup=cleanup, commit_cleanup=not lock) if row else None


def _active_print(db, printer_id):
    return db.execute(text("SELECT id, scheduled_job_id, economic_uid FROM print_jobs WHERE printer_id=:id "
                           "AND ended_at IS NULL AND status IN ('running','paused','printing') ORDER BY id DESC LIMIT 1"),
                      {"id": printer_id}).first()


def _matches_attempt(db, printer_id, record):
    if record["print_job_id"] is None:
        if record["action"] != "resume":
            return True
        if not record.get("resume_paused_at"):
            return False
        sent = datetime.fromisoformat(record["sent_at"])
        # SQLite's julianday rounds to milliseconds. Resolve ties at stored precision.
        newer = ("julianday(started_at) >= julianday(:sent)" if db.get_bind().dialect.name == "sqlite"
                 else "started_at > :sent")
        starts = db.execute(text(f"SELECT started_at FROM print_jobs WHERE printer_id=:id AND {newer}"),
                            {"id": printer_id, "sent": record["sent_at"]}).scalars()
        return not any(as_utc_datetime(start) > sent for start in starts)
    active = _active_print(db, printer_id)
    if active is None:
        return record["action"] == "stop"
    return (active.id == record["print_job_id"] and
            (not record["print_attempt_uid"] or active.economic_uid == record["print_attempt_uid"]))


def _begin(db, printer_id, record, outcome):
    """Reconcile after device I/O, serializing only the bookkeeping."""
    previous = _read(db, printer_id, lock=True) if outcome != "failed" else None
    # Resolve the attempt at send time, even if it ended during the short wait.
    # SQLite stores both space- and T-separated timestamps.
    sent = datetime.fromisoformat(record["sent_at"])
    sqlite = db.get_bind().dialect.name == "sqlite"
    started = "julianday(started_at) <= julianday(:sent)" if sqlite else "started_at <= :sent"
    ended = "julianday(ended_at) >= julianday(:sent)" if sqlite else "ended_at >= :sent"
    attempt = db.execute(text(
        f"SELECT id, scheduled_job_id, economic_uid, started_at FROM print_jobs WHERE printer_id=:id AND {started} "
        f"AND ((ended_at IS NULL AND status IN ('running','paused','printing')) OR {ended}) "
        "ORDER BY id DESC LIMIT 1"), {"id": printer_id, "sent": record["sent_at"]}).first()
    if attempt and as_utc_datetime(attempt.started_at) <= sent:
        record.update(job_id=attempt.scheduled_job_id, print_job_id=attempt.id, print_attempt_uid=attempt.economic_uid)
    if outcome == "failed":
        _finish(db, printer_id, record, outcome, datetime.now(timezone.utc))
        db.commit()
        return None, outcome
    if outcome == "confirmed" and not _matches_attempt(db, printer_id, record):
        outcome = "pending"
    # A newer command may already have finished and cleared its pending row.
    latest = previous
    if latest is None:
        from core.models import AuditLog
        latest = db.query(AuditLog.details).filter(
            AuditLog.entity_type == "printer", AuditLog.entity_id == printer_id,
            AuditLog.action.in_([f"printer.{action}_requested" for action in DEADLINES]),
            AuditLog.details["sent_at"].as_string() > record["sent_at"],
        ).order_by(AuditLog.id.desc()).limit(1).scalar()
    action = record["action"]
    if isinstance(latest, dict) and latest.get("sent_at", "") > record["sent_at"]:
        log_audit(db, f"printer.{action}_requested", "printer", printer_id, record)
        log_audit(db, f"printer.{action}_replaced", "printer", printer_id,
                  {**record, "replaced_by": latest["action"]})
        db.commit()
        return previous, outcome
    if previous:
        log_audit(db, f"printer.{previous['action']}_replaced", "printer", printer_id,
                  {**previous, "replaced_by": action})
    log_audit(db, f"printer.{action}_requested", "printer", printer_id, record)
    if outcome == "pending":
        write_config_text(db, _key(printer_id), json.dumps(record))
    else:
        _finish(db, printer_id, record, outcome, datetime.now(timezone.utc))
    db.commit()
    return (record if outcome == "pending" else None), outcome


def _finish(db, printer_id, record, outcome, now):
    if outcome != "failed":
        db.execute(text("DELETE FROM system_config WHERE key=:k"), {"k": _key(printer_id)})
    elapsed = (now - datetime.fromisoformat(record["sent_at"])).total_seconds()
    log_audit(db, f"printer.{record['action']}_{outcome}", "printer", printer_id,
              {**record, "elapsed_seconds": round(elapsed, 2)})


def observe_control(db, printer_id, name, status=None, *, observed_at=None, now=None):
    """Only consume the same generation whose poll we observed. Alert and removal commit together."""
    record = _read(db, printer_id)
    if not record:
        return
    now = now or datetime.now(timezone.utc)
    expired = now >= datetime.fromisoformat(record["deadline_at"])
    observed_at = observed_at or now
    if observed_at < datetime.fromisoformat(record["sent_at"]):
        return
    unbound_resume = record["action"] == "resume" and record["print_job_id"] is None
    saw_pause = unbound_resume and status and status.raw_print_state == "paused" and not record.get("resume_paused_at")
    confirmed = status is not None and control_confirmed(record["action"], status)
    if unbound_resume and record.get("resume_paused_at"):
        confirmed = confirmed and observed_at > datetime.fromisoformat(record["resume_paused_at"])
    if not expired and (record["awaiting_reply"] or not (confirmed or saw_pause)):
        return
    if _read(db, printer_id, lock=True) != record:
        return
    if saw_pause and not expired:
        record["resume_paused_at"] = observed_at.isoformat()
        write_config_text(db, _key(printer_id), json.dumps(record))
        db.commit()
        return
    confirmed = confirmed and _matches_attempt(db, printer_id, record)
    if not confirmed and not expired:
        return
    _finish(db, printer_id, record, "confirmed" if confirmed else "unconfirmed", now)
    if confirmed and record["automatic"] and record.get("detection_id") is not None:
        db.execute(text("UPDATE vision_detections SET status='auto_paused' WHERE id=:id AND printer_id=:pid "
                        "AND print_job_id=:job AND status='pending'"),
                   {"id": record["detection_id"], "pid": printer_id, "job": record["print_job_id"]})
    plan = None
    if not confirmed:
        from modules.notifications.services import stage_control_timeout
        action = record["action"]
        minutes = DEADLINES[action] / 60
        message = (f"{name}: ODIN sent {action.title()} but the printer did not confirm it "
                   f"within {minutes:g} minutes. Check the printer.")
        # A job may have been deleted while the printer was offline. Keep its
        # original identity in the audit, and scope the alert to live resources.
        job_id = record["job_id"]
        if job_id is not None:
            job_id = db.execute(text("SELECT id FROM jobs WHERE id=:id"), {"id": job_id}).scalar()
        plan = stage_control_timeout(db, printer_id, job_id, message)
    db.commit()
    if plan is not None:
        from modules.notifications.services import deliver_staged_alert
        deliver_staged_alert(db, plan)


def expired_printer_ids(db, now):
    """Include printers without a polling session, including after a daemon restart."""
    rows = db.execute(text("SELECT c.key, CAST(c.value AS TEXT), p.id FROM system_config c "
                           "LEFT JOIN printers p ON c.key='pending_control:' || CAST(p.id AS TEXT) "
                           "WHERE c.key LIKE 'pending!_control:%' ESCAPE '!'")).all()
    expired = []
    for key, value, printer_id in rows:
        record = _decode(db, key, value)
        if record and (printer_id is None or now >= datetime.fromisoformat(record["deadline_at"])):
            expired.append(int(key.split(":")[1]))
    return expired


def _rollback_bookkeeping(db, printer_id):
    try:
        db.rollback()
    except Exception as exc:
        log.warning("Control bookkeeping rollback failed for printer %s (%s)", printer_id, type(exc).__name__)


def send_control(db, printer, action, actor, *, live_check=None, detection_id=None, expected_print_job_id=None, via=None):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    from core.crypto import decrypt
    automatic = live_check is not None
    proof = control_proof(printer, action)
    if proof == "not_working":
        if automatic:
            return {"outcome": "unavailable"}
        raise HTTPException(409, detail={"code": "control_not_available",
            "message": f"{action.title()} from ODIN does not work on this printer model. Use the printer's touchscreen."})
    if action == "resume" and (refusal := command_refusal(printer, "resume_print")):
        raise HTTPException(409, detail=refusal)
    key = decrypt(printer.api_key) if printer.api_key else ""
    adapter = MoonrakerPrinter(printer.api_host, api_key=key, model=printer.model, machine_type=printer.machine_type)
    if automatic and not live_check():
        return {"outcome": "skipped"}
    printer_id = printer.id
    if automatic:
        try:
            previous = _read(db, printer_id, cleanup=False)
            if previous and previous["action"] == "pause" and _inside_deadline(previous):
                return {"outcome": "skipped", "reason": "pending_pause"}
        except Exception as exc:
            log.warning("Could not check pending pause for printer %s (%s)", printer_id, type(exc).__name__)
        finally:
            _rollback_bookkeeping(db, printer_id)
    if expected_print_job_id is not None:
        attempt = _active_print(db, printer_id)
        db.rollback()
        if attempt is None or attempt.id != expected_print_job_id:
            raise HTTPException(409, "Detection no longer matches the active print")
    sent = datetime.now(timezone.utc)
    if automatic:
        success = adapter.pause_print()
    else:
        success = adapter.run_control(action)
    reply, confirmed = adapter.last_control
    failed = not success and reply.outcome != "no_reply"
    outcome = "confirmed" if confirmed else "pending"
    record = dict(action=action, sent_at=sent.isoformat(),
                  deadline_at=(sent + timedelta(seconds=DEADLINES[action])).isoformat(),
                  actor_user_id=actor.get("id"), actor_token_id=actor.get("_token_id"),
                  automatic=automatic, job_id=None, print_job_id=None, print_attempt_uid=None, awaiting_reply=False)
    if action == "resume" and (paused_at := getattr(adapter, "resume_paused_at", None)):
        record["resume_paused_at"] = paused_at.isoformat()
    if detection_id is not None:
        record["detection_id"] = detection_id
    if via is not None:
        record["via"] = via
    if proof == "unproven":
        record["unproven"] = True
    current = None
    attempts = 2 if action == "stop" and not failed else 1
    for attempt in range(attempts):
        try:
            if attempt:
                if db.in_transaction():  # The rollback itself failed.
                    db.invalidate()
                # A commit can succeed even when its acknowledgement is lost.
                from core.models import AuditLog
                recorded = db.query(AuditLog.id).filter(
                    AuditLog.entity_type == "printer", AuditLog.entity_id == printer_id,
                    AuditLog.action == "printer.stop_requested",
                    AuditLog.details["sent_at"].as_string() == record["sent_at"]).first()
                if recorded:
                    current = _read(db, printer_id)
                    if current and current["sent_at"] == record["sent_at"]:
                        outcome = "pending"
                    break
            current, outcome = _begin(db, printer_id, record.copy(), "failed" if failed else outcome)
            break
        except Exception as exc:
            _rollback_bookkeeping(db, printer_id)
            if attempt + 1 == attempts:
                if action == "resume" and not failed:
                    outcome = "pending"  # The attempt could not be verified.
                log.log(logging.ERROR if attempts == 2 else logging.WARNING,
                        "Could not record %s for printer %s user_id=%s token_id=%s sent_at=%s (%s)",
                        action, printer_id, record["actor_user_id"], record["actor_token_id"],
                        record["sent_at"], type(exc).__name__)
    if failed:
        message = ("The printer is unreachable." if reply.outcome == "unreachable" else
                   f"The printer rejected {action.title()} (HTTP {reply.status}). {reply.error}".strip())
        if automatic:
            return {"outcome": "failed"}
        if action == "stop":
            raise HTTPException(503, detail=message)
        raise OdinError(ErrorCode.upstream_unavailable, message, status=503,
                        retriable=reply.outcome == "unreachable", extra={"outcome": reply.outcome})
    result = {"success": True, "outcome": outcome, "message": (CONFIRMED if outcome == "confirmed" else PENDING)[action]}
    if proof == "unproven":
        result["unproven"] = True
    if current and not current["awaiting_reply"] and _inside_deadline(current):
        result["pending_control"] = public_record(current)
    return result
