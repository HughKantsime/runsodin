"""Filament check: per-job result for the dispatch dialog, and the superadmin setting."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import check_org_access, require_role, require_superadmin
from modules.printers import filament_check

router = APIRouter(tags=['Filament check'])


class FilamentCheckSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Optional[Literal['off', 'warn', 'hold']] = None
    margin_pct: Optional[float] = Field(default=None, ge=0, le=filament_check.MAX_MARGIN_PCT, allow_inf_nan=False)


def _settings(db) -> dict:
    return filament_check.read_settings(db.connection())


@router.get('/config/filament-check')
def get_filament_check_settings(current_user: dict = Depends(require_superadmin()),
                                db: Session = Depends(get_db)):
    return _settings(db)


@router.put('/config/filament-check')
def set_filament_check_settings(body: FilamentCheckSettings,
                                current_user: dict = Depends(require_superadmin()),
                                db: Session = Depends(get_db)):
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, 'Choose a mode or a margin to change')
    try:
        before = _settings(db)
        filament_check.write_settings(db, mode=changes.get('mode'), margin_pct=changes.get('margin_pct'))
        after = {**before, **changes}
        log_audit(db, 'filament_check.updated', 'settings', details={'before': before, 'after': after})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _settings(db)


@router.get('/jobs/{job_id}/filament-check')
def get_job_filament_check(job_id: int = Path(gt=0), printer_id: Optional[int] = Query(default=None, gt=0),
                           current_user: dict = Depends(require_role('viewer', scope='read:jobs')),
                           db: Session = Depends(get_db)):
    """What dispatch would decide for this job on this printer right now."""
    job = db.execute(text('SELECT id, printer_id, charged_to_org_id FROM jobs WHERE id = :id'),
                     {'id': job_id}).mappings().first()
    if job is None or not check_org_access(current_user, job['charged_to_org_id']):
        raise HTTPException(404, 'Job not found')
    from core.controlled_parts import assert_access
    assert_access(db, 'job', job_id, current_user)
    target = printer_id if printer_id is not None else job['printer_id']
    if target is None:
        raise HTTPException(400, 'Choose a printer to check')
    printer = db.execute(text('SELECT org_id, shared FROM printers WHERE id = :id'),
                         {'id': target}).mappings().first()
    owner = printer is not None and check_org_access(current_user, printer['org_id'])
    if printer is None or not (owner or printer['shared']):
        raise HTTPException(404, 'Printer not found')
    result = filament_check.check_job(db.connection(), job_id, target)
    # A printer reached only because it is shared: the verdict, not the owner's spool id or weight.
    return result if owner else filament_check.hide_spool_details(result)
