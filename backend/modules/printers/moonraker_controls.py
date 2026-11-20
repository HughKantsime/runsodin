"""Track one outstanding print control per Moonraker printer in the reminder key/value store."""
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import bindparam, text

from core.audit import log_audit
from core.errors import ErrorCode, OdinError
from core.system_config_values import config_text, write_config_text
from modules.printers.adapters.moonraker import control_confirmed
from modules.printers.support_levels import command_refusal, control_proof

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


def enrich_controls(db, printers):
    keys = {_key(p.id): p for p in printers if p.api_type == "moonraker"}
    for printer in keys.values():
        printer.pending_control = None
    if keys:
        rows = db.execute(text("SELECT key, value FROM system_config WHERE key IN :keys")
                          .bindparams(bindparam("keys", expanding=True)), {"keys": list(keys)})
        for key, value in rows:
            record = json.loads(config_text(value))
            if not record["awaiting_reply"]:
                keys[key].pending_control = public_record(record)
    return printers


def _read(db, printer_id, *, lock=False):
    key = _key(printer_id)
    if lock:
        # Serialize even the first insertion. Release before any device I/O.
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT id FROM printers WHERE id=:id FOR UPDATE"), {"id": printer_id})
        else:
            db.execute(text("UPDATE system_config SET key=key WHERE key=:k"), {"k": key})
    value = db.execute(text("SELECT value FROM system_config WHERE key=:k"), {"k": key}).scalar()
    return json.loads(config_text(value)) if value is not None else None


def _active_print(db, printer_id):
    return db.execute(text("SELECT id, scheduled_job_id, economic_uid FROM print_jobs WHERE printer_id=:id "
                           "AND status IN ('running','paused','printing') ORDER BY id DESC LIMIT 1"),
                      {"id": printer_id}).first()


def _begin(db, printer, action, actor, proof, automatic):
    previous = _read(db, printer.id, lock=True)
    if automatic and previous and previous["action"] in ("pause", "stop"):
        db.rollback()
        return None
    sent = datetime.now(timezone.utc)
    attempt = _active_print(db, printer.id)
    record = dict(action=action, sent_at=sent.isoformat(),
                  deadline_at=(sent + timedelta(seconds=DEADLINES[action])).isoformat(),
                  actor_user_id=actor.get("id"), actor_token_id=actor.get("_token_id"),
                  automatic=automatic, job_id=attempt.scheduled_job_id if attempt else None,
                  print_job_id=attempt.id if attempt else None,
                  print_attempt_uid=attempt.economic_uid if attempt else None, awaiting_reply=True)
    if proof == "unproven":
        record["unproven"] = True
    if previous:
        log_audit(db, f"printer.{previous['action']}_replaced", "printer", printer.id,
                  {**previous, "replaced_by": action})
    write_config_text(db, _key(printer.id), json.dumps(record))
    log_audit(db, f"printer.{action}_requested", "printer", printer.id, record)
    db.commit()
    return record


def _finish(db, printer_id, record, outcome, now):
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
    if not expired and (record.get("awaiting_reply") or status is None
                        or (observed_at or now) < datetime.fromisoformat(record["sent_at"])
                        or not control_confirmed(record["action"], status)):
        return
    if _read(db, printer_id, lock=True) != record:
        return
    if not expired and record["print_job_id"]:
        active = _active_print(db, printer_id)
        if active and (active.id != record["print_job_id"] or
                       (record["print_attempt_uid"] and record["print_attempt_uid"] != active.economic_uid)):
            return
    _finish(db, printer_id, record, "unconfirmed" if expired else "confirmed", now)
    plan = None
    if expired:
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
    rows = db.execute(text("SELECT key,value FROM system_config WHERE key LIKE 'pending_control:%'"))
    return [int(key.split(":")[1]) for key, value in rows
            if now >= datetime.fromisoformat(json.loads(config_text(value))["deadline_at"])]


def send_control(db, printer, action, actor, *, live_check=None):
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
    record = _begin(db, printer, action, actor, proof, automatic)
    if record is None:
        return {"outcome": "skipped"}
    if automatic:
        success = adapter.pause_print()
    else:
        success = adapter.run_control(action)
    reply, confirmed = adapter.last_control
    failed = not success and reply.outcome != "no_reply"
    outcome = "confirmed" if confirmed else "pending"
    current = _read(db, printer_id, lock=True)
    if current == record:
        now = datetime.now(timezone.utc)
        if failed or confirmed:
            _finish(db, printer_id, record, "failed" if failed else outcome, now)
            current = None
        else:
            current = {**record, "awaiting_reply": False}
            write_config_text(db, _key(printer_id), json.dumps(current))
    db.commit()
    if failed:
        message = ("The printer is unreachable." if reply.outcome == "unreachable" else
                   f"The printer rejected {action.title()} (HTTP {reply.status}). {reply.error}".strip())
        if automatic:
            return {"outcome": "failed"}
        if action == "stop":
            raise HTTPException(503, detail=message)
        raise OdinError(ErrorCode.upstream_unavailable, message, status=503,
                        retriable=reply.outcome == "unreachable", extra={"outcome": reply.outcome})
    result = {"success": True, "outcome": outcome, "message": (CONFIRMED if confirmed else PENDING)[action]}
    if proof == "unproven":
        result["unproven"] = True
    if current and not current["awaiting_reply"]:
        result["pending_control"] = public_record(current)
    return result
