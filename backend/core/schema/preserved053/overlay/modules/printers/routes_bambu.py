"""Bambu integration routes — test-connection, sync-ams, filament types, manual slot assignment, unmatched slots."""
from core.base import slot_filament_value

import logging
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

from core.spoolman_settings import get_spoolman_url
from core.db import get_db
from core.rbac import check_org_access, require_role, require_action
from core.config import settings
import core.crypto as crypto
from modules.printers.models import Printer, FilamentSlot
from modules.printers.ledger_display import ensure_local_filaments
from modules.inventory.models import Spool, FilamentLibrary
from core.base import FilamentType, SpoolStatus

# Bambu Lab Integration
try:
    from modules.printers.bambu_integration import (
        test_bambu_connection, sync_ams_filaments, slot_to_dict,
        map_bambu_filament_type, BAMBU_FILAMENT_TYPE_MAP, MQTT_AVAILABLE,
    )
    BAMBU_AVAILABLE = MQTT_AVAILABLE
except ImportError:
    BAMBU_AVAILABLE = False

log = logging.getLogger("odin.api")
router = APIRouter()


class BambuConnectionTest(PydanticBaseModel):
    ip_address: str
    serial_number: str
    access_code: str


class AMSSlotResponse(PydanticBaseModel):
    ams_id: int
    tray_id: int
    slot_number: int
    filament_type_raw: str
    filament_type: str
    color_hex: Optional[str]
    spool_change_pending: bool = False
    observed_state: Optional[str] = None
    preset: Optional[str] = None
    remaining_percent: Optional[float]
    brand: Optional[str]
    is_empty: bool
    match_source: Optional[str] = None
    color_name: Optional[str] = None
    matched_filament_id: Optional[str] = None
    matched_filament_name: Optional[str] = None


class BambuSyncResult(PydanticBaseModel):
    success: bool
    printer_name: Optional[str] = None
    slots: List[AMSSlotResponse] = []
    message: str
    slots_updated: int = 0
    unmatched_slots: List[int] = []


class ManualSlotAssignment(PydanticBaseModel):
    filament_library_id: Optional[int] = None
    filament_type: Optional[str] = None
    color: Optional[str] = None
    color_hex: Optional[str] = None
    brand: Optional[str] = None


# ====================================================================
# Bambu Integration
# ====================================================================

def _owned_printer(db, printer_id, current_user):
    """Load a printer in the caller's org scope; 404 for missing or foreign printers."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer


@router.post("/bambu/test-connection", tags=["Bambu"])
async def test_bambu_printer_connection(request: BambuConnectionTest, current_user: dict = Depends(require_action("printers.edit", scope="write:printers"))):
    """Test connection to a Bambu Lab printer via local MQTT."""
    if not BAMBU_AVAILABLE:
        raise HTTPException(status_code=501, detail="Bambu integration not available. Install: pip install paho-mqtt")

    from modules.printers.route_utils import _check_ssrf_blocklist
    _check_ssrf_blocklist(request.ip_address)

    result = test_bambu_connection(
        ip_address=request.ip_address,
        serial_number=request.serial_number,
        access_code=request.access_code,
    )
    return result


@router.post("/printers/{printer_id}/bambu/sync-ams", response_model=BambuSyncResult, tags=["Bambu"])
async def sync_bambu_ams(printer_id: int, current_user: dict = Depends(require_action("printers.edit", scope="write:printers")), db: Session = Depends(get_db)):
    """Use the shared observed-identity writer for the legacy sync endpoint."""
    from modules.printers.routes_ams import sync_ams_state
    from modules.printers.schemas import FilamentSlotResponse
    result = sync_ams_state(printer_id, current_user=current_user, db=db)
    printer = _owned_printer(db, printer_id, current_user)
    slots = []
    for row in printer.filament_slots:
        slot = FilamentSlotResponse.model_validate(row)
        slots.append(dict(ams_id=(slot.slot_number - 1) // 4, tray_id=(slot.slot_number - 1) % 4,
                          slot_number=slot.slot_number, filament_type_raw=slot.material_type or '',
                          filament_type=slot.filament_type.value, color_hex=slot.color_hex,
                          remaining_percent=slot.remaining, brand=slot.brand, color_name=slot.color,
                          is_empty=slot.filament_type == FilamentType.EMPTY,
                          spool_change_pending=slot.spool_change_pending, observed_state=slot.observed_state, preset=slot.preset))
    return BambuSyncResult(success=True, printer_name=printer.name, slots=slots,
                           slots_updated=result['slots_synced'], message=f"Synced {result['slots_synced']} slots from AMS")


@router.get("/bambu/filament-types", tags=["Bambu"])
async def list_bambu_filament_types():
    """List Bambu filament type codes and their mappings."""
    if not BAMBU_AVAILABLE:
        return {"error": "Bambu integration not available"}
    return {"bambu_to_normalized": BAMBU_FILAMENT_TYPE_MAP}


@router.patch("/printers/{printer_id}/slots/{slot_number}/manual-assign", tags=["Bambu"])
async def manual_slot_assignment(
    printer_id: int,
    slot_number: int,
    assignment: ManualSlotAssignment,
    current_user: dict = Depends(require_action("printers.slots", scope="write:printers")), db: Session = Depends(get_db)
):
    """Manually assign filament to a slot when auto-matching fails."""
    _owned_printer(db, printer_id, current_user)
    ensure_local_filaments(printer_id)
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()

    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")

    if assignment.filament_library_id:
        lib_entry = db.query(FilamentLibrary).filter(FilamentLibrary.id == assignment.filament_library_id).first()
        if not lib_entry:
            raise HTTPException(status_code=404, detail="Library filament not found")
        try:
            slot.filament_type = FilamentType(slot_filament_value(lib_entry.material))
        except ValueError:
            slot.filament_type = FilamentType(slot_filament_value(FilamentType.OTHER))
        slot.color = lib_entry.name
        slot.color_hex = lib_entry.color_hex
        slot.spoolman_spool_id = f"lib_{lib_entry.id}"
    else:
        if assignment.filament_type:
            try:
                slot.filament_type = FilamentType(slot_filament_value(assignment.filament_type))
            except ValueError:
                slot.filament_type = FilamentType(slot_filament_value(FilamentType.OTHER))
        if assignment.color:
            slot.color = assignment.color
        if assignment.color_hex:
            slot.color_hex = assignment.color_hex

    slot.loaded_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(slot)

    return {
        "success": True, "slot_number": slot_number,
        "filament_type": slot.filament_type.value if slot.filament_type else None,
        "color": slot.color, "color_hex": slot.color_hex,
    }


@router.get("/printers/{printer_id}/unmatched-slots", tags=["Bambu"])
async def get_unmatched_slots(printer_id: int, current_user: dict = Depends(require_role("viewer", scope="read")),
                              db: Session = Depends(get_db)):
    """Get slots that need manual filament assignment."""
    printer = _owned_printer(db, printer_id, current_user)

    unmatched = []
    for slot in printer.filament_slots:
        needs_attention = False
        reason = []
        if slot.filament_type == FilamentType.OTHER:
            needs_attention = True
            reason.append("Unknown filament type")
        if not slot.spoolman_spool_id and slot.color_hex:
            needs_attention = True
            reason.append("No library match")
        if needs_attention:
            unmatched.append({
                "slot_number": slot.slot_number,
                "current_type": slot.filament_type.value if slot.filament_type else None,
                "color": slot.color, "color_hex": slot.color_hex,
                "reason": ", ".join(reason),
            })

    return {
        "printer_id": printer_id, "printer_name": printer.name,
        "unmatched_count": len(unmatched), "slots": unmatched,
    }
