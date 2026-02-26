"""Bambu integration routes — test-connection, sync-ams, filament types, manual slot assignment, unmatched slots."""

import logging
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role
from core.config import settings
import core.crypto as crypto
from modules.printers.models import Printer, FilamentSlot
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
    remaining_percent: Optional[int]
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

@router.post("/bambu/test-connection", tags=["Bambu"])
async def test_bambu_printer_connection(request: BambuConnectionTest, current_user: dict = Depends(require_role("operator"))):
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
async def sync_bambu_ams(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Sync AMS filament slots from a Bambu Lab printer."""
    if not BAMBU_AVAILABLE:
        raise HTTPException(status_code=501, detail="Bambu integration not available")

    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if printer.api_type != "bambu":
        raise HTTPException(status_code=400, detail=f"Printer is type '{printer.api_type}', not 'bambu'")

    if not printer.api_host:
        raise HTTPException(status_code=400, detail="Printer has no Bambu config (api_host empty)")

    try:
        parts = crypto.decrypt(printer.api_key).split("|")
        if len(parts) != 2:
            raise ValueError()
        serial_number, access_code = parts
        ip_address = printer.api_host
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Bambu config. Expected: ip|serial|access_code")

    library_filaments = db.query(FilamentLibrary).all()
    library_list = [
        {"id": f"lib_{f.id}", "brand": f.brand, "name": f.name, "material": f.material, "color_hex": f.color_hex}
        for f in library_filaments
    ]

    spoolman_list = []
    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=5)
                if resp.status_code == 200:
                    for spool in resp.json():
                        filament = spool.get("filament", {})
                        spoolman_list.append({
                            "id": f"spool_{spool['id']}",
                            "brand": filament.get("vendor", {}).get("name", "Unknown"),
                            "name": filament.get("name", "Unknown"),
                            "material": filament.get("material", "PLA"),
                            "color_hex": filament.get("color_hex"),
                        })
        except Exception:
            pass

    result = sync_ams_filaments(
        ip_address=ip_address,
        serial_number=serial_number,
        access_code=access_code,
        library_filaments=library_list,
        spoolman_spools=spoolman_list,
    )

    if not result.success:
        raise HTTPException(status_code=502, detail=result.message)

    slots_updated = 0
    unmatched_slots = []
    slot_responses = []

    for slot_info in result.slots:
        slot_dict = slot_to_dict(slot_info)
        slot_responses.append(AMSSlotResponse(**slot_dict))

        if slot_info.is_empty:
            continue

        db_slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == printer_id,
            FilamentSlot.slot_number == slot_info.slot_number,
        ).first()

        if not db_slot:
            db_slot = FilamentSlot(
                printer_id=printer_id,
                slot_number=slot_info.slot_number,
                filament_type=FilamentType.EMPTY,
            )
            db.add(db_slot)

        try:
            db_slot.filament_type = FilamentType(slot_info.mapped_type)
        except ValueError:
            try:
                db_slot.filament_type = FilamentType.from_bambu_code(slot_info.filament_type)
            except Exception:
                db_slot.filament_type = FilamentType.OTHER
            unmatched_slots.append(slot_info.slot_number)

        if slot_info.mapped_type in ["PLA_SUPPORT", "SUPPORT", "PVA", "HIPS", "BVOH"]:
            db_slot.color_hex = "#F5F5F5"
            db_slot.color = "Natural"
        else:
            db_slot.color_hex = slot_info.color_hex
            db_slot.color = slot_info.color_name or slot_info.brand
        db_slot.loaded_at = datetime.now(timezone.utc)

        if slot_info.matched_filament:
            db_slot.spoolman_spool_id = slot_info.matched_filament.get('id')

        slots_updated += 1

    db.commit()

    return BambuSyncResult(
        success=True,
        printer_name=result.printer_name,
        slots=slot_responses,
        message=f"Synced {slots_updated} slots from AMS",
        slots_updated=slots_updated,
        unmatched_slots=unmatched_slots,
    )


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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Manually assign filament to a slot when auto-matching fails."""
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
            slot.filament_type = FilamentType(lib_entry.material.upper())
        except ValueError:
            slot.filament_type = FilamentType.OTHER
        slot.color = lib_entry.name
        slot.color_hex = lib_entry.color_hex
        slot.spoolman_spool_id = f"lib_{lib_entry.id}"
    else:
        if assignment.filament_type:
            try:
                slot.filament_type = FilamentType(assignment.filament_type.upper())
            except ValueError:
                slot.filament_type = FilamentType.OTHER
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
async def get_unmatched_slots(printer_id: int, db: Session = Depends(get_db)):
    """Get slots that need manual filament assignment."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

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
