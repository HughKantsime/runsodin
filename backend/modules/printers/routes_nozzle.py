"""Nozzle lifecycle routes — install, retire, and history."""

from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role
from core.dependencies import get_current_user
from modules.printers.models import Printer, NozzleLifecycle
from modules.printers.schemas import NozzleInstall, NozzleLifecycleResponse

log = logging.getLogger("odin.api")
router = APIRouter()


@router.get("/printers/{printer_id}/nozzle", tags=["Telemetry"])
def get_current_nozzle(printer_id: int, current_user: dict = Depends(require_role("viewer")),
                       db: Session = Depends(get_db)):
    """Get the currently installed nozzle for a printer."""
    nozzle = db.query(NozzleLifecycle).filter(
        NozzleLifecycle.printer_id == printer_id,
        NozzleLifecycle.removed_at.is_(None),
    ).first()
    if not nozzle:
        return None
    return NozzleLifecycleResponse.model_validate(nozzle)


@router.post("/printers/{printer_id}/nozzle", tags=["Telemetry"])
def install_nozzle(printer_id: int, data: NozzleInstall,
                   current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Install a new nozzle (auto-retires the previous one)."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    current = db.query(NozzleLifecycle).filter(
        NozzleLifecycle.printer_id == printer_id,
        NozzleLifecycle.removed_at.is_(None),
    ).first()
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
                  current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Retire a specific nozzle."""
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
def get_nozzle_history(printer_id: int, current_user: dict = Depends(require_role("viewer")),
                       db: Session = Depends(get_db)):
    """Get all nozzles (past and present) for a printer."""
    nozzles = db.query(NozzleLifecycle).filter(
        NozzleLifecycle.printer_id == printer_id,
    ).order_by(NozzleLifecycle.installed_at.desc()).all()
    return [NozzleLifecycleResponse.model_validate(n) for n in nozzles]
