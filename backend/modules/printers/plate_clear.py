"""Plate-clear confirmation that does not need a waiting job.

`POST /printers/{id}/plate-cleared` used to write nothing unless a job was in
waiting_plate_clear, so `plate_clear_confirmed_at` stayed empty on farms that clear plates
by hand. The confirmation is recorded as the existing `bed_clear_confirmed` timeline event
on the printer's last finished print (no new table or column).
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

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
    # Stored times mix forms (naive UTC, ISO with "T" or an offset), so they are compared as
    # parsed instants, never as text, across every finished print on this printer (three small
    # columns; an old low-numbered job can be the one that finished last).
    recent = db.execute(text(
        f"SELECT id, charged_to_org_id, actual_end FROM jobs WHERE printer_id=:p AND status IN {_ENDED} "
        "AND actual_end IS NOT NULL"), {"p": printer.id}).all()
    dated = [(as_utc(row[2]), row[0], row) for row in recent if as_utc(row[2]) is not None]
    if not dated:
        return None
    ended, _, job = max(dated, key=lambda item: (item[0], item[1]))
    # Written only against a job this caller may read: its organization and, for controlled
    # work, its audience (the job routes' own rule).
    from core.controlled_parts import allowed
    if not check_org_access(actor, job[1]) or not allowed(db, "job", job[0], actor):
        return None
    for (at,) in db.execute(text(
            "SELECT occurred_at FROM job_timeline_events WHERE job_id=:j AND printer_id=:p "
            "AND kind='bed_clear_confirmed' ORDER BY occurred_at DESC"), {"j": job[0], "p": printer.id}):
        known = as_utc(at)
        if known is not None and known >= ended:
            return known  # already confirmed after this print ended: one row per print
    now = datetime.now(timezone.utc)
    user_id = actor.get("id") if isinstance(actor, dict) and type(actor.get("id")) is int else None
    db.execute(text(
        "INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,actor_user_id,actor_token_id,occurred_at) "
        "VALUES(:id,:job,:printer,:org,'bed_clear_confirmed',:detail,:user,NULL,:at)"),
        {"id": uuid.uuid4().hex, "job": job[0], "printer": printer.id, "org": job[1],
         "detail": json.dumps({"source": "plate_cleared", "waiting_job": False}), "user": user_id, "at": now})
    return now
