"""Explicit specific-printer pins.

A pin is stored in the job's own target columns, never in jobs.printer_id
(the scheduler's current assignment), so every reset to pending keeps it:
target_type='specific' with target_filter=<printer id> means "only this
printer". target_type='specific' with no target_filter (the default for every
job) is not a pin. Deliberately not a foreign key: a deleted printer leaves the
job pinned and visibly blocked, never free to run elsewhere.
"""
from typing import Optional

from sqlalchemy import text

ISSUE_MISSING = "pinned_printer_missing"
ISSUE_INACTIVE = "pinned_printer_inactive"


def pinned_printer_id(target_type, target_filter) -> Optional[int]:
    if (target_type or "specific") != "specific" or target_filter is None:
        return None
    value = str(target_filter).strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def job_pin(job) -> Optional[int]:
    return pinned_printer_id(getattr(job, "target_type", None), getattr(job, "target_filter", None))


def pin_columns(printer_id: Optional[int]) -> dict:
    """Column values that set (or, with None, clear) an explicit pin."""
    return {"target_type": "specific", "target_filter": str(int(printer_id)) if printer_id else None}


def pin_issue(db, printer_id: Optional[int]) -> Optional[str]:
    if printer_id is None or db is None:
        return None
    row = db.execute(text("SELECT is_active FROM printers WHERE id=:id"), {"id": printer_id}).first()
    if row is None:
        return ISSUE_MISSING
    if not row[0]:
        return ISSUE_INACTIVE
    return None
