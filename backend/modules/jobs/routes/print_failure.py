"""Documented operator correction endpoints; never publish printer events."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_action
from modules.jobs.print_failure import change

router = APIRouter(tags=['Print Jobs'])


class MarkPrintFailed(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: Literal['no_filament', 'filament_supply', 'first_layer', 'came_loose', 'under_extrusion', 'printer_fault', 'power_connection', 'other']
    note: str = Field(default='', max_length=1000)
    filament_used_g: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)
    no_material_used: bool = Field(default=False, strict=True)

    @field_validator('note')
    @classmethod
    def plain_note(cls, value):
        if any(ord(char) < 32 and char not in '\n\t' for char in value) or '<' in value or '>' in value:
            raise ValueError('Use plain text for the note.')
        return value.strip()


def _apply(db, print_id, user, body=None):
    try:
        result = change(db, print_id, user, body)
        db.commit()
        return result
    except OperationalError:
        db.rollback()
        raise HTTPException(409, 'This print is being updated. Reload and try again.') from None
    except Exception:
        db.rollback()
        raise


@router.post('/print-jobs/{print_id}/mark-failed')
def mark_print_failed(print_id: int, body: MarkPrintFailed, db: Session = Depends(get_db),
                      user: dict = Depends(require_action('jobs.edit', scope='write:jobs'))):
    """Mark one finished cancelled print as failed, with a required reason.

    Optional grams must not exceed recorded use. Inventory-backed, externally
    managed, multi-filament and retained-cost corrections are refused when exact
    restoration is unavailable. No alert or notification is sent.
    """
    return _apply(db, print_id, user, body)


@router.post('/print-jobs/{print_id}/undo-failure')
def undo_print_failure(print_id: int, db: Session = Depends(get_db),
                       user: dict = Depends(require_action('jobs.edit', scope='write:jobs'))):
    """Back to cancelled: restore the saved outcome and figures exactly.

    Refuses missing corrections, changed history or inventory that can no longer
    be restored. The original and corrected values remain in the audit log.
    """
    return _apply(db, print_id, user)
