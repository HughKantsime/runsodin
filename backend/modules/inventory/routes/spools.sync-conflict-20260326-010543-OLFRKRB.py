"""Spool CRUD and action endpoints (load/unload/use/weigh/drying)."""

from typing import Optional
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, _get_org_filter, get_org_scope, check_org_access
from core.base import SpoolStatus
from modules.inventory.models import Spool, SpoolUsage
from modules.printers.models import FilamentSlot, Printer
from ._helpers import (
    SpoolCreate, SpoolUpdate,
    SpoolLoadRequest, SpoolUseRequest, SpoolWeighRequest,
)

log = logging.getLogger("odin.api")
router = APIRouter(prefix="/spools", tags=["Spools"])


# ====================================================================
# Spool CRUD
# ====================================================================

@router.get("/low-stock", tags=["Spools"])
def low_stock_spools(
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """List spools below their low-stock threshold."""
    spools = db.query(Spool).filter(Spool.status == SpoolStatus.ACTIVE).all()
    result = []
    for s in spools:
        threshold = s.low_stock_threshold_g or 50
        remaining = s.remaining_weight_g or 0
        if remaining < threshold:
            result.append({
                "id": s.id,
                "filament_brand": s.filament.brand if s.filament else None,
                "filament_name": s.filament.name if s.filament else None,
                "filament_material": s.filament.material if s.filament else None,
                "remaining_weight_g": remaining,
                "low_stock_threshold_g": threshold,
                "location_printer_id": s.location_printer_id,
            })
    return result


@router.get("", tags=["Spools"])
def list_spools(
    status: Optional[str] = None,
    filament_id: Optional[int] = None,
    printer_id: Optional[int] = None,
    org_id: Optional[int] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all spools with optional filters."""
    query = db.query(Spool)

    if status:
        query = query.filter(Spool.status == status)
    if filament_id:
        query = query.filter(Spool.filament_id == filament_id)
    if printer_id:
        query = query.filter(Spool.location_printer_id == printer_id)

    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if effective_org is not None:
        query = query.filter((Spool.org_id == effective_org) | (Spool.org_id == None))

    spools = query.all()

    result = []
    for s in spools:
        spool_dict = {
            "id": s.id,
            "filament_id": s.filament_id,
            "qr_code": s.qr_code,
            "initial_weight_g": s.initial_weight_g,
            "remaining_weight_g": s.remaining_weight_g,
            "spool_weight_g": s.spool_weight_g,
            "percent_remaining": s.percent_remaining,
            "price": s.price,
            "purchase_date": s.purchase_date,
            "vendor": s.vendor,
            "lot_number": s.lot_number,
            "status": s.status.value if s.status else None,
            "location_printer_id": s.location_printer_id,
            "location_slot": s.location_slot,
            "storage_location": s.storage_location,
            "notes": s.notes,
            "created_at": s.created_at,
            "filament_brand": s.filament.brand if s.filament else None,
            "filament_name": s.filament.name if s.filament else None,
            "filament_material": s.filament.material if s.filament else None,
            "filament_color_hex": s.color_hex or (s.filament.color_hex if s.filament else None),
            "pa_profile": s.pa_profile,
            "low_stock_threshold_g": s.low_stock_threshold_g,
            "is_low_stock": (s.remaining_weight_g or 0) < (s.low_stock_threshold_g or 50),
        }
        result.append(spool_dict)

    return result


@router.post("", tags=["Spools"])
def create_spool(spool: SpoolCreate, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Create a new spool."""
    from modules.inventory.models import FilamentLibrary
    # Verify filament exists
    filament = db.query(FilamentLibrary).filter(FilamentLibrary.id == spool.filament_id).first()
    if not filament:
        raise HTTPException(status_code=404, detail="Filament not found")

    # Generate QR code
    import uuid
    qr_code = f"SPL-{uuid.uuid4().hex[:8].upper()}"

    db_spool = Spool(
        filament_id=spool.filament_id,
        qr_code=qr_code,
        initial_weight_g=spool.initial_weight_g,
        remaining_weight_g=spool.initial_weight_g,
        spool_weight_g=spool.spool_weight_g,
        price=spool.price,
        purchase_date=spool.purchase_date,
        vendor=spool.vendor,
        lot_number=spool.lot_number,
        storage_location=spool.storage_location,
        notes=spool.notes,
        status=SpoolStatus.ACTIVE,
        org_id=current_user.get("group_id") if current_user else None,
    )
    db.add(db_spool)
    db.commit()
    db.refresh(db_spool)

    log_audit(db, "create", "spool", db_spool.id, {"filament_id": spool.filament_id, "qr_code": db_spool.qr_code})
    return {
        "id": db_spool.id,
        "qr_code": db_spool.qr_code,
        "message": "Spool created",
    }


@router.get("/{spool_id}", tags=["Spools"])
def get_spool(spool_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get a single spool with details."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if current_user and not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    return {
        "id": spool.id,
        "filament_id": spool.filament_id,
        "qr_code": spool.qr_code,
        "initial_weight_g": spool.initial_weight_g,
        "remaining_weight_g": spool.remaining_weight_g,
        "spool_weight_g": spool.spool_weight_g,
        "percent_remaining": spool.percent_remaining,
        "price": spool.price,
        "purchase_date": spool.purchase_date,
        "vendor": spool.vendor,
        "lot_number": spool.lot_number,
        "status": spool.status.value if spool.status else None,
        "location_printer_id": spool.location_printer_id,
        "location_slot": spool.location_slot,
        "storage_location": spool.storage_location,
        "notes": spool.notes,
        "created_at": spool.created_at,
        "updated_at": spool.updated_at,
        "filament_brand": spool.filament.brand if spool.filament else None,
        "filament_name": spool.filament.name if spool.filament else None,
        "filament_material": spool.filament.material if spool.filament else None,
        "filament_color_hex": spool.filament.color_hex if spool.filament else None,
        "usage_history": [
            {
                "id": u.id,
                "weight_used_g": u.weight_used_g,
                "used_at": u.used_at,
                "job_id": u.job_id,
                "notes": u.notes,
            }
            for u in spool.usage_history
        ],
    }


@router.patch("/{spool_id}", tags=["Spools"])
def update_spool(spool_id: int, updates: SpoolUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update spool details."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    update_data = updates.model_dump(exclude_unset=True)

    if "status" in update_data:
        update_data["status"] = SpoolStatus(update_data["status"])

    for field, value in update_data.items():
        setattr(spool, field, value)

    db.commit()
    db.refresh(spool)

    return {"success": True, "id": spool.id}


@router.delete("/{spool_id}", tags=["Spools"])
def delete_spool(spool_id: int, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Delete a spool (or archive it)."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    # Archive instead of delete
    spool.status = SpoolStatus.ARCHIVED
    db.commit()

    return {"success": True, "message": "Spool archived"}


# ====================================================================
# Spool Actions (load/unload/use/weigh)
# ====================================================================

@router.post("/{spool_id}/load", tags=["Spools"])
def load_spool(spool_id: int, request: SpoolLoadRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Load a spool into a printer slot."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    printer = db.query(Printer).filter(Printer.id == request.printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if request.slot_number < 1 or request.slot_number > printer.slot_count:
        raise HTTPException(status_code=400, detail=f"Invalid slot number (1-{printer.slot_count})")

    # Unload any existing spool in that slot
    existing = db.query(Spool).filter(
        Spool.location_printer_id == request.printer_id,
        Spool.location_slot == request.slot_number,
    ).first()
    if existing and existing.id != spool_id:
        existing.location_printer_id = None
        existing.location_slot = None

    # Update spool location
    spool.location_printer_id = request.printer_id
    spool.location_slot = request.slot_number
    spool.storage_location = None

    # Update filament slot assignment
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == request.printer_id,
        FilamentSlot.slot_number == request.slot_number,
    ).first()
    if slot:
        slot.assigned_spool_id = spool_id
        slot.spool_confirmed = True

    db.commit()

    return {
        "success": True,
        "spool_id": spool_id,
        "printer": printer.name,
        "slot": request.slot_number,
    }


@router.post("/{spool_id}/unload", tags=["Spools"])
def unload_spool(
    spool_id: int,
    storage_location: Optional[str] = None,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Unload a spool from printer to storage."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    # Clear slot assignment
    if spool.location_printer_id and spool.location_slot:
        slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == spool.location_printer_id,
            FilamentSlot.slot_number == spool.location_slot,
        ).first()
        if slot and slot.assigned_spool_id == spool_id:
            slot.assigned_spool_id = None
            slot.spool_confirmed = False

    spool.location_printer_id = None
    spool.location_slot = None
    spool.storage_location = storage_location

    db.commit()

    return {"success": True, "message": "Spool unloaded"}


@router.post("/{spool_id}/use", tags=["Spools"])
def use_spool(spool_id: int, request: SpoolUseRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Record filament usage from a spool."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    # Deduct weight
    spool.remaining_weight_g = max(0, spool.remaining_weight_g - request.weight_used_g)

    # Check if empty
    if spool.remaining_weight_g <= 0:
        spool.status = SpoolStatus.EMPTY

    # Record usage
    usage = SpoolUsage(
        spool_id=spool.id,
        job_id=request.job_id,
        weight_used_g=request.weight_used_g,
        notes=request.notes,
    )
    db.add(usage)
    db.commit()

    log_audit(db, "use", "spool", spool_id, {"weight_used_g": request.weight_used_g, "remaining": spool.remaining_weight_g})
    return {
        "success": True,
        "remaining_weight_g": spool.remaining_weight_g,
        "percent_remaining": spool.percent_remaining,
        "status": spool.status.value,
    }


@router.post("/{spool_id}/weigh", tags=["Spools"])
def weigh_spool(spool_id: int, request: SpoolWeighRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update spool weight from scale measurement."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    # Calculate net filament weight
    old_weight = spool.remaining_weight_g
    new_weight = max(0, request.gross_weight_g - spool.spool_weight_g)
    spool.remaining_weight_g = new_weight

    # Record as usage if weight decreased
    if new_weight < old_weight:
        usage = SpoolUsage(
            spool_id=spool.id,
            weight_used_g=old_weight - new_weight,
            notes="Manual weigh adjustment",
        )
        db.add(usage)

    # Check if empty
    if spool.remaining_weight_g <= 10:  # Less than 10g = effectively empty
        spool.status = SpoolStatus.EMPTY

    db.commit()

    return {
        "success": True,
        "old_weight_g": old_weight,
        "new_weight_g": new_weight,
        "percent_remaining": spool.percent_remaining,
    }


# ====================================================================
# Drying
# ====================================================================

@router.post("/{spool_id}/dry", tags=["Spools"])
def log_drying_session(
    spool_id: int,
    duration_hours: float,
    temp_c: Optional[float] = None,
    method: str = "dryer",
    notes: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator")),
):
    """Log a filament drying session for a spool."""
    from modules.inventory.models import DryingLog
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    drying_log = DryingLog(
        spool_id=spool_id,
        duration_hours=duration_hours,
        temp_c=temp_c,
        method=method,
        notes=notes,
    )
    db.add(drying_log)
    db.commit()
    db.refresh(drying_log)

    log_audit(db, "create", "drying_log", drying_log.id, {"spool_id": spool_id, "duration_hours": duration_hours, "method": method})

    return {
        "id": drying_log.id,
        "spool_id": drying_log.spool_id,
        "dried_at": drying_log.dried_at.isoformat() if drying_log.dried_at else None,
        "duration_hours": drying_log.duration_hours,
        "temp_c": drying_log.temp_c,
        "method": drying_log.method,
        "notes": drying_log.notes,
    }


@router.get("/{spool_id}/drying-history", tags=["Spools"])
def get_drying_history(spool_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get drying session history for a spool."""
    from modules.inventory.models import DryingLog
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    logs = (
        db.query(DryingLog)
        .filter(DryingLog.spool_id == spool_id)
        .order_by(DryingLog.dried_at.desc())
        .all()
    )
    return [
        {
            "id": l.id,
            "dried_at": l.dried_at.isoformat() if l.dried_at else None,
            "duration_hours": l.duration_hours,
            "temp_c": l.temp_c,
            "method": l.method,
            "notes": l.notes,
        }
        for l in logs
    ]
