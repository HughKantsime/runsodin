"""Explicit specific-printer pins.

A pin is stored in the job's own target columns, never in jobs.printer_id
(the scheduler's current assignment), so every reset to pending keeps it:

    target_type='specific', target_filter='<printer id>'   pinned to that printer
    target_type='specific', target_filter=NULL/''           no pin (any printer)
    target_type='specific', target_filter='deleted:<id>'    the pinned printer was deleted;
                                                            the job waits for a new choice

Deliberately not a foreign key: a deleted printer must leave the job visibly
blocked, never silently retargeted (SQLite may reuse the printer id).

Issues reported on a pinned job (JobResponse.pinned_printer_issue, and the
scheduler's queue-skipped alert, whose reason is the same issue name):

    pinned_printer_missing       the id does not exist (legacy data)
    pinned_printer_deleted       the printer was deleted (pin cleared on delete)
    pinned_printer_inactive      the printer is disabled
    pinned_printer_inaccessible  the job's org may no longer use it (ownership check)
    pinned_target_invalid        a legacy, unparseable target_filter (treated as no pin)
"""
from typing import Optional

from sqlalchemy import event, text
from sqlalchemy.orm import Session

ISSUE_MISSING = "pinned_printer_missing"
ISSUE_DELETED = "pinned_printer_deleted"
ISSUE_INACTIVE = "pinned_printer_inactive"
ISSUE_INACCESSIBLE = "pinned_printer_inaccessible"
ISSUE_INVALID = "pinned_target_invalid"
BLOCKING = frozenset({ISSUE_MISSING, ISSUE_DELETED, ISSUE_INACTIVE, ISSUE_INACCESSIBLE})
DELETED_PREFIX = "deleted:"


def is_printer_id_text(value) -> bool:
    text_value = str(value).strip()
    return bool(text_value) and text_value.isascii() and text_value.isdigit() and int(text_value) > 0


def parse_target(target_type, target_filter):
    """Return (pinned printer id or None, issue or None) for stored target columns."""
    if (target_type or "specific") != "specific" or target_filter is None:
        return None, None
    value = str(target_filter).strip()
    if not value:
        return None, None
    if value.startswith(DELETED_PREFIX):
        return None, ISSUE_DELETED
    if is_printer_id_text(value):
        return int(value), None
    return None, ISSUE_INVALID


def pinned_printer_id(target_type, target_filter) -> Optional[int]:
    return parse_target(target_type, target_filter)[0]


def job_pin(job) -> Optional[int]:
    return pinned_printer_id(getattr(job, "target_type", None), getattr(job, "target_filter", None))


def pin_columns(printer_id: Optional[int]) -> dict:
    """Column values that set (or, with None, clear) an explicit pin."""
    return {"target_type": "specific", "target_filter": str(int(printer_id)) if printer_id else None}


def _issue_for(row, job_org) -> Optional[str]:
    if row is None:
        return ISSUE_MISSING
    is_active, org_id, shared = row
    from modules.jobs.scheduler import _printer_authority
    authority = _printer_authority(org_id, shared, True)
    if not is_active:
        return ISSUE_INACTIVE
    from core.dispatch_identity import ownership_refusal
    if authority is None or ownership_refusal({"charged_to_org_id": job_org}, authority):
        return ISSUE_INACCESSIBLE
    return None


def pin_issue(db, printer_id: Optional[int], job_org=None) -> Optional[str]:
    """Why the pinned printer cannot take the job (None when it can)."""
    if printer_id is None or db is None:
        return None
    row = db.execute(text("SELECT is_active, org_id, shared FROM printers WHERE id=:id"), {"id": printer_id}).first()
    return _issue_for(tuple(row) if row is not None else None, job_org)


def job_issue(db, job) -> Optional[str]:
    """Issue for a job's stored target, including deleted/invalid pins."""
    pin, issue = parse_target(getattr(job, "target_type", None), getattr(job, "target_filter", None))
    if issue:
        return issue
    return pin_issue(db, pin, getattr(job, "charged_to_org_id", None))


# ---- one printer read per request for JobResponse (no N+1) -----------------
_CACHE = "odin_pin_printer_states"


def cached_job_issue(session, job) -> Optional[str]:
    pin, issue = parse_target(getattr(job, "target_type", None), getattr(job, "target_filter", None))
    if issue or pin is None or session is None:
        return issue
    states = session.info.get(_CACHE)
    if states is None:
        states = {row[0]: (row[1], row[2], row[3]) for row in
                  session.execute(text("SELECT id, is_active, org_id, shared FROM printers")).all()}
        session.info[_CACHE] = states
    return _issue_for(states.get(pin), getattr(job, "charged_to_org_id", None))


def _drop_cache(session, *_args):
    session.info.pop(_CACHE, None)


for _name in ("after_commit", "after_rollback", "after_soft_rollback", "after_flush"):
    event.listen(Session, _name, _drop_cache)


def clear_pins_for_deleted_printer(db, printer_id: int) -> list:
    """On printer delete: mark pins to it as deleted (never left to match a reused id)."""
    rows = db.execute(text("SELECT id FROM jobs WHERE COALESCE(target_type,'specific')='specific' "
                           "AND TRIM(COALESCE(target_filter,''))=:pid"), {"pid": str(int(printer_id))}).scalars().all()
    if rows:
        db.execute(text("UPDATE jobs SET target_filter=:marker WHERE COALESCE(target_type,'specific')='specific' "
                        "AND TRIM(COALESCE(target_filter,''))=:pid"),
                   {"marker": f"{DELETED_PREFIX}{int(printer_id)}", "pid": str(int(printer_id))})
    return list(rows)
