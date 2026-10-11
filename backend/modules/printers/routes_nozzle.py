"""Nozzle lifecycle routes — install, retire, and history."""

from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role, check_org_access
from modules.printers.models import Printer, NozzleLifecycle
from modules.printers.schemas import NozzleInstall, NozzleLifecycleResponse

log = logging.getLogger("odin.api")
router = APIRouter()



def _nozzle_printer(printer_id: int, db: Session, current_user: dict, *, write: bool = False) -> Printer:
    def authorized(printer):
        return printer is not None and (
            check_org_access(current_user, printer.org_id) or (not write and printer.shared)
        )
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not authorized(printer):
        raise HTTPException(status_code=404, detail="Printer not found")
    if write:
        # PostgreSQL parent-row lock and SQLite writer lock, held until commit/rollback.
        db.execute(text("UPDATE printers SET id=id WHERE id=:pid"), {"pid": printer_id})
        printer = db.query(Printer).filter(Printer.id == printer_id).populate_existing().first()
        if not authorized(printer):
            raise HTTPException(status_code=404, detail="Printer not found")
    return printer


def _current_nozzle(printer_id: int, db: Session):
    active = db.query(NozzleLifecycle).filter(
        NozzleLifecycle.printer_id == printer_id,
        NozzleLifecycle.removed_at.is_(None),
    ).limit(2).all()
    if len(active) > 1:
        raise HTTPException(status_code=409, detail="Multiple active nozzle records require administrator review")
    return active[0] if active else None


@router.get("/printers/{printer_id}/nozzle", tags=["Telemetry"])
def get_current_nozzle(printer_id: int, current_user: dict = Depends(require_role("viewer", scope="read:printers")),
                       db: Session = Depends(get_db)):
    """Get the currently installed nozzle for a printer."""
    _nozzle_printer(printer_id, db, current_user)
    nozzle = _current_nozzle(printer_id, db)
    if not nozzle:
        return None
    return NozzleLifecycleResponse.model_validate(nozzle)


@router.post("/printers/{printer_id}/nozzle", tags=["Telemetry"])
def install_nozzle(printer_id: int, data: NozzleInstall,
                   current_user: dict = Depends(require_role("operator", scope="write:printers")), db: Session = Depends(get_db)):
    """Install a new nozzle (auto-retires the previous one)."""
    _nozzle_printer(printer_id, db, current_user, write=True)
    current = _current_nozzle(printer_id, db)
    if current:
        current.removed_at = datetime.now(timezone.utc)
    nozzle = NozzleLifecycle(
        printer_id=printer_id,
        nozzle_type=data.nozzle_type,
        nozzle_diameter=data.nozzle_diameter,
        notes=data.notes,
    )
    db.add(nozzle)
    db.commit()
    db.refresh(nozzle)
    return NozzleLifecycleResponse.model_validate(nozzle)


@router.patch("/printers/{printer_id}/nozzle/{nozzle_id}/retire", tags=["Telemetry"])
def retire_nozzle(printer_id: int, nozzle_id: int,
                  current_user: dict = Depends(require_role("operator", scope="write:printers")), db: Session = Depends(get_db)):
    """Retire a specific nozzle."""
    _nozzle_printer(printer_id, db, current_user, write=True)
    _current_nozzle(printer_id, db)
    nozzle = db.query(NozzleLifecycle).filter(
        NozzleLifecycle.id == nozzle_id,
        NozzleLifecycle.printer_id == printer_id,
    ).first()
    if not nozzle:
        raise HTTPException(status_code=404, detail="Nozzle not found")
    if nozzle.removed_at:
        raise HTTPException(status_code=400, detail="Nozzle already retired")
    nozzle.removed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(nozzle)
    return NozzleLifecycleResponse.model_validate(nozzle)


@router.get("/printers/{printer_id}/nozzle/history", tags=["Telemetry"])
def get_nozzle_history(printer_id: int, current_user: dict = Depends(require_role("viewer", scope="read:printers")),
                       db: Session = Depends(get_db)):
    """Get all nozzles (past and present) for a printer."""
    _nozzle_printer(printer_id, db, current_user)
    nozzles = db.query(NozzleLifecycle).filter(
        NozzleLifecycle.printer_id == printer_id,
    ).order_by(NozzleLifecycle.installed_at.desc(), NozzleLifecycle.id.desc()).all()
    return [NozzleLifecycleResponse.model_validate(n) for n in nozzles]
