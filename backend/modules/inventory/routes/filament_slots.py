"""Printer filament slot assignment and attention endpoints."""

import logging
from modules.inventory.slot_assignments import assign_slot
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role, require_action, check_org_access
from modules.inventory.models import Spool
from modules.printers.models import FilamentSlot, Printer
from core.interfaces.filament_ownership import ensure_local_filament_management

log = logging.getLogger("odin.api")
router = APIRouter(prefix="/printers", tags=["Filament Slots"])


@router.post("/{printer_id}/slots/{slot_number}/assign", tags=["Spools"])
def assign_spool_to_slot(
    printer_id: int,
    slot_number: int,
    spool_id: int,
    current_user: dict = Depends(require_action("printers.slots", scope="write:printers")), db: Session = Depends(get_db)
):
    """Assign a spool to a printer slot."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not (check_org_access(current_user, printer.org_id) or printer.shared):
        raise HTTPException(status_code=404, detail="Printer not found")
    ensure_local_filament_management(printer_id)
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")

    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    assign_slot(db, slot, spool_id, current_user)
    slot.spool_confirmed = False

    db.commit()

    return {"success": True, "message": "Spool assigned, awaiting confirmation"}


@router.post("/{printer_id}/slots/{slot_number}/confirm", tags=["Spools"])
def confirm_slot_assignment(
    printer_id: int,
    slot_number: int,
    current_user: dict = Depends(require_action("printers.slots", scope="write:printers")), db: Session = Depends(get_db)
):
    """Confirm the spool assignment for a slot."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not (check_org_access(current_user, printer.org_id) or printer.shared):
        raise HTTPException(status_code=404, detail="Printer not found")
    ensure_local_filament_management(printer_id)
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")

    if not slot.assigned_spool_id:
        raise HTTPException(status_code=400, detail="No spool assigned to confirm")

    slot.spool_confirmed = True
    db.commit()

    return {"success": True, "message": "Spool assignment confirmed"}


@router.get("/{printer_id}/slots/needs-attention", tags=["Spools"])
def get_slots_needing_attention(printer_id: int, current_user: dict = Depends(require_role("viewer", scope="read:printers")), db: Session = Depends(get_db)):
    """Get slots that need spool confirmation or have mismatches."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    issues = []
    for slot in printer.filament_slots:
        slot_issues = []

        # No spool assigned but slot has filament
        if not slot.assigned_spool_id and slot.color_hex:
            slot_issues.append("No spool assigned")

        # Spool assigned but not confirmed
        if slot.assigned_spool_id and not slot.spool_confirmed:
            slot_issues.append("Awaiting confirmation")

        # Spool assigned but type/color mismatch
        if slot.assigned_spool and slot.assigned_spool.filament:
            spool_fil = slot.assigned_spool.filament
            if slot.color_hex and spool_fil.color_hex:
                # Simple mismatch check
                if slot.color_hex.lower().replace("#", "") != spool_fil.color_hex.lower().replace("#", ""):
                    slot_issues.append(f"Color mismatch: slot={slot.color_hex}, spool={spool_fil.color_hex}")

        if slot_issues:
            issues.append({
                "slot_number": slot.slot_number,
                "issues": slot_issues,
                "current_type": slot.filament_type.value if slot.filament_type else None,
                "current_color": slot.color,
                "current_color_hex": slot.color_hex,
                "assigned_spool_id": slot.assigned_spool_id,
                "spool_confirmed": slot.spool_confirmed,
            })

    return {
        "printer_id": printer_id,
        "printer_name": printer.name,
        "slots_needing_attention": len(issues),
        "slots": issues,
    }
