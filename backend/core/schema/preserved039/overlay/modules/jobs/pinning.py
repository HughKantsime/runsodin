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


LIVE_PIN_STATUSES = ("pending", "scheduled", "submitted")


class DeletedPins(tuple):
    """(marked job ids, live job ids) from clear_pins_for_deleted_printer."""
    __slots__ = ()

    def __new__(cls, marked, live):
        return super().__new__(cls, (list(marked), list(live)))

    marked = property(lambda self: self[0])
    live = property(lambda self: self[1])


def clear_pins_for_deleted_printer(db, printer_id: int, *, chunk: int = 500) -> DeletedPins:
    """On printer delete: mark every job's pin to it as deleted:<id>, whatever the status.

    target_filter is the job's *target*, not where it printed (print_jobs keeps
    that), so marking finished jobs loses no history -- and it is
    what stops a later reset/resubmit from reviving the pin onto a printer that
    reuses the id. ``live`` lists the jobs that could still be scheduled
    (pending/scheduled/submitted): only their owners are alerted. IN lists are
    chunked so they stay bounded.
    """
    pid = str(int(printer_id))
    rows = db.execute(text("SELECT id, status FROM jobs WHERE COALESCE(target_type,'specific')='specific' "
                           "AND TRIM(COALESCE(target_filter,''))=:pid ORDER BY id"), {"pid": pid}).all()
    ids = [row[0] for row in rows]
    live = [row[0] for row in rows if (getattr(row[1], "value", row[1]) or "") in LIVE_PIN_STATUSES]
    from sqlalchemy import bindparam
    statement = text("UPDATE jobs SET target_filter=:marker WHERE id IN :ids "
                     "AND TRIM(COALESCE(target_filter,''))=:pid").bindparams(bindparam("ids", expanding=True))
    step = max(1, int(chunk))
    for start in range(0, len(ids), step):
        db.execute(statement, {"marker": f"{DELETED_PREFIX}{int(printer_id)}", "pid": pid,
                               "ids": ids[start:start + step]})
    return DeletedPins(ids, live)


# Validated pin helpers for the job creation routes (modules/jobs/routes/jobs_crud.py).

def _target_printer_id(db, current_user, printer_id, target_type, target_filter=None):
    """Validate an explicit specific-printer choice; return the id to pin (or None).

    The choice is printer_id (what the Jobs UI sends) or, for API callers, a
    numeric target_filter with target_type 'specific'.
    """
    from fastapi import HTTPException
    from core.rbac import check_org_access
    from modules.printers.models import Printer
    if (target_type or "specific") != "specific":
        return None
    if printer_id is None and target_filter is not None and str(target_filter).strip():
        if not is_printer_id_text(target_filter):  # ASCII digits only; no Unicode digits, signs or junk
            raise HTTPException(status_code=422, detail="A specific target_filter must be a printer id")
        printer_id = int(str(target_filter).strip())
    if printer_id is None:
        return None
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not (check_org_access(current_user, printer.org_id) or printer.shared):
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer.id


def _target_columns(job, pinned):
    if (job.target_type or "specific") == "specific":
        return pin_columns(pinned)
    return {"target_type": job.target_type, "target_filter": job.target_filter}


def _pin_checked_changes(db, current_user, job, updates):
    """PATCH /jobs/{id}: the JobUpdate changes with printer access checked and the pin reconciled."""
    from fastapi import HTTPException
    from core.rbac import check_org_access
    from modules.printers.models import Printer
    for candidate in (updates.printer_id, updates.pinned_printer_id):
        if candidate is not None:
            printer = db.query(Printer).filter(Printer.id == candidate).first()
            if not printer or not (check_org_access(current_user, printer.org_id) or printer.shared):
                raise HTTPException(status_code=404, detail="Printer not found")
    changes = updates.model_dump(exclude_unset=True)
    from core.print_file_binding import job_file_id, target_printer, require_bound_target
    file_id = job_file_id(db, changes.get("model_id", job.model_id), job.print_file_id)
    bound = target_printer(db, file_id)
    if bound is not None:
        if job.print_file_id is None and changes.get("model_id", job.model_id) != job.model_id:
            from core.errors import OdinError, ErrorCode
            raise OdinError(ErrorCode.resource_conflict, "Schedule this bound file from the model library", status=409)
        for field in ("printer_id", "pinned_printer_id"):
            if field in changes:
                require_bound_target(db, file_id, changes[field])
        changes.update(pin_columns(bound))
    # printer_id is the scheduler's assignment, never a pin. On a pinned job it
    # must agree with the pin this PATCH leaves in place: a conflicting
    # printer_id is refused (409) rather than silently retargeting the job --
    # change the pin itself with pinned_printer_id (or move the job).
    if changes.get("printer_id") is not None:
        pin, pin_problem = parse_target(job.target_type, job.target_filter)
        if "pinned_printer_id" in changes:
            pin, pin_problem = changes["pinned_printer_id"], None
        if pin_problem == ISSUE_DELETED:
            raise HTTPException(409, "The job's pinned printer was deleted; choose a printer with pinned_printer_id")
        if pin is not None and int(pin) != int(changes["printer_id"]):
            raise HTTPException(409, f"The job is pinned to printer {int(pin)}; change the pin with "
                                     "pinned_printer_id or move the job")
    # A pin changes only when pinned_printer_id is sent explicitly. A printer id
    # pins (replacing any model/protocol target: the user chose this printer);
    # null clears an existing specific pin but never touches a model/protocol target.
    if "pinned_printer_id" in changes:
        chosen = changes.pop("pinned_printer_id")
        if chosen is not None or (job.target_type or "specific") == "specific":
            changes.update(pin_columns(chosen))
    return changes
