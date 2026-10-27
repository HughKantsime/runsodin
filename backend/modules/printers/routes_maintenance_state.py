"""Audited administrative maintenance state; tenant-visible reads."""
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.models import SystemConfig
from core.dependencies import log_audit
from core.rbac import require_role, check_org_access
from core.printer_maintenance import maintenance_key, read_maintenance
from modules.printers.models import Printer

router = APIRouter(tags=['Printers'])


class MaintenanceState(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    state: Literal['ok', 'warn', 'hold']
    note: str = Field(default='', max_length=1000)

    @model_validator(mode='after')
    def reason_required(self):
        if self.state != 'ok' and not self.note:
            raise ValueError('A maintenance note is required for warn or hold')
        return self


def visible(db, printer_id, user, *, write=False):
    printer = db.get(Printer, printer_id)
    if printer is None or not (check_org_access(user, printer.org_id) or (not write and printer.shared)):
        raise HTTPException(404, 'Printer not found')
    return printer


@router.get('/printers/{printer_id}/maintenance-state')
def get_maintenance(printer_id: int = Path(gt=0), db: Session = Depends(get_db),
                    current_user=Depends(require_role('viewer', scope='read:printers'))):
    visible(db, printer_id, current_user)
    return read_maintenance(db, printer_id)


@router.put('/printers/{printer_id}/maintenance-state')
def set_maintenance(body: MaintenanceState, printer_id: int = Path(gt=0), db: Session = Depends(get_db),
                    current_user=Depends(require_role('admin', scope='write:printers'))):
    try:
        visible(db, printer_id, current_user, write=True)
        # Same printer row as dispatch admission: serializes edits with admission.
        db.execute(text('UPDATE printers SET is_active=is_active WHERE id=:id'), {'id': printer_id})
        db.expire_all()
        visible(db, printer_id, current_user, write=True)
        before = read_maintenance(db, printer_id)
        value = {**body.model_dump(), 'set_by': current_user.get('id'),
                 'set_by_name': current_user.get('username'), 'set_at': datetime.now(timezone.utc).isoformat()}
        row = db.get(SystemConfig, maintenance_key(printer_id))
        if row is None:
            db.add(SystemConfig(key=maintenance_key(printer_id), value=value))
        else:
            row.value = value
        log_audit(db, 'printer.maintenance', 'printer', printer_id, {'before': before, 'after': value})
        db.commit()
        return value
    except Exception:
        db.rollback()
        raise
