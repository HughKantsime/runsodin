"""Plate-clear confirmation that does not need a waiting job.

`POST /printers/{id}/plate-cleared` used to write nothing unless a job was in
waiting_plate_clear, so `plate_clear_confirmed_at` stayed empty on farms that clear plates
by hand. Confirmations use the job timeline when linked to a job, or one guarded
printer-owned system_config value for a print started at the printer.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from core.audit import log_audit
from core.printer_display_facts import plate_clear_key, stored_plate_clear_times
from core.printer_utils import is_printing_state
from core.rbac import check_org_access

_ENDED = "('completed','failed','cancelled')"


def as_utc(value) -> Optional[datetime]:
    """Stored times are naive UTC or ISO strings (SQLite); None when unreadable."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def record_plate_clear(db, printer, actor) -> Optional[datetime]:
    """Record the confirmation for `printer`; the caller commits.

    Returns its time, or None when there is nothing to confirm: the printer is printing or
    paused, it has no finished print, or that print belongs to another organization.
    """
    state = str(printer.gcode_state or "").strip().upper()
    if is_printing_state(printer.gcode_state) or state in ("PAUSE", "PAUSED"):
        return None
    if db.execute(text("SELECT 1 FROM jobs WHERE printer_id=:p AND status IN ('printing','paused') LIMIT 1"),
                  {"p": printer.id}).first():
        return None
    if not check_org_access(actor, printer.org_id) or db.execute(text(
            "SELECT 1 FROM print_jobs WHERE printer_id=:p AND status='running' LIMIT 1"),
            {"p": printer.id}).first():
        return None
    # Monitor end times decide linked prints. Keep operator-finished jobs when their
    # print row was closed with an unknown end. Preserve the archived-submission filter.
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- static status list; printer id is bound
    recent = db.execute(text(
        f"SELECT p.id,p.ended_at,p.id,p.scheduled_job_id FROM print_jobs p WHERE p.printer_id=:p AND p.status IN {_ENDED} "
        "AND p.ended_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM education_submissions s "
        "WHERE s.job_id=p.scheduled_job_id AND s.archived_at IS NOT NULL) "
        "UNION ALL SELECT j.id,j.actual_end,NULL,j.id FROM jobs j "
        f"WHERE j.printer_id=:p AND j.status IN {_ENDED} AND j.actual_end IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM print_jobs p WHERE p.scheduled_job_id=j.id AND p.ended_at IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=j.id AND s.archived_at IS NOT NULL)"),
        {"p": printer.id}).all()
    # Times mix naive UTC and ISO offsets. The higher id breaks end-time ties; for an
    # equal id across the two tables, prefer the monitor's print record.
    dated = [(as_utc(row[1]), row[0], row[2] is not None, row[2], row[3])
             for row in recent if as_utc(row[1]) is not None]
    if not dated:
        return None
    ended, _, _, print_id, job_id = max(dated, key=lambda item: item[:3])
    from core.controlled_parts import allowed
    org_id = printer.org_id
    if job_id is not None:
        job = db.execute(text("SELECT charged_to_org_id FROM jobs WHERE id=:id"), {"id": job_id}).first()
        if job is None or not check_org_access(actor, job[0]) or not allowed(db, "job", job_id, actor):
            return None
        org_id = job[0]
    # Both start sources must agree with the reader, even for a print closed without an end.
    starts = [begun for begun in (as_utc(row[0]) for row in db.execute(text(
        "SELECT actual_start FROM jobs WHERE printer_id=:p AND actual_start IS NOT NULL "
        "UNION ALL SELECT started_at FROM print_jobs WHERE printer_id=:p AND started_at IS NOT NULL"),
        {"p": printer.id})) if begun is not None]
    known_times = list(stored_plate_clear_times(db, [printer.id], actor).values())
    for at, owner, identifier in db.execute(text(
            "SELECT e.occurred_at,j.charged_to_org_id,j.id FROM job_timeline_events e JOIN jobs j ON j.id=e.job_id "
            "WHERE e.printer_id=:p AND e.kind='bed_clear_confirmed' AND NOT EXISTS "
            "(SELECT 1 FROM education_submissions s WHERE s.job_id=j.id AND s.archived_at IS NOT NULL)"),
            {"p": printer.id}):
        if check_org_access(actor, owner) and allowed(db, "job", identifier, actor):
            known_times.append(at)
    known = max((at for at in (as_utc(value) for value in known_times) if at is not None), default=None)
    if known is not None and known >= ended and not any(begun > known for begun in starts):
        return known
    now = datetime.now(timezone.utc)
    user_id = actor.get("id") if isinstance(actor, dict) and type(actor.get("id")) is int else None
    token_id = actor.get("_token_id") if isinstance(actor, dict) and type(actor.get("_token_id")) is int else None
    if job_id is not None:
        db.execute(text(
            "INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,actor_user_id,actor_token_id,occurred_at) "
            "VALUES(:id,:job,:printer,:org,'bed_clear_confirmed',:detail,:user,:token,:at)"),
            {"id": uuid.uuid4().hex, "job": job_id, "printer": printer.id, "org": org_id,
             "detail": json.dumps({"source": "plate_cleared", "waiting_job": False}),
             "user": user_id, "token": token_id, "at": now})
    else:
        # Use the stored creation value, as display facts do, to guard against reused ids.
        created_at = db.execute(text("SELECT created_at FROM printers WHERE id=:p"), {"p": printer.id}).scalar_one()
        db.execute(text("INSERT INTO system_config(key,value) VALUES(:key,:value) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
                   {"key": plate_clear_key(printer.id), "value": json.dumps({"at": now.isoformat(),
                    "print_job_id": print_id, "org_id": org_id, "printer_created_at": str(created_at)})})
    log_audit(db, "printer.plate_clear_confirmed", "printer", printer.id,
              {"printer_id": printer.id, "print_job_id": print_id, "job_id": job_id,
               "actor_token_id": token_id})
    return now
