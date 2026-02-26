"""AMS environment routes — AMS telemetry, current readings, RFID refresh, slot configuration."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role
from modules.printers.models import Printer, FilamentSlot

log = logging.getLogger("odin.api")
router = APIRouter()


# ====================================================================
# AMS Environment
# ====================================================================

@router.get("/printers/{printer_id}/ams/environment", tags=["AMS"])
async def get_ams_environment(
    printer_id: int,
    hours: int = Query(default=24, ge=1, le=168),
    unit: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Get AMS humidity/temperature history for charts."""
    query = """
        SELECT ams_unit, humidity, temperature, recorded_at
        FROM ams_telemetry
        WHERE printer_id = :printer_id
        AND recorded_at >= datetime('now', :hours_ago)
    """
    params = {"printer_id": printer_id, "hours_ago": f"-{hours} hours"}

    if unit is not None:
        query += " AND ams_unit = :unit"
        params["unit"] = unit

    query += " ORDER BY recorded_at ASC"

    rows = db.execute(text(query), params).fetchall()

    units = {}
    for row in rows:
        u = row[0]
        if u not in units:
            units[u] = []
        units[u].append({"humidity": row[1], "temperature": row[2], "time": row[3]})

    return {"printer_id": printer_id, "hours": hours, "units": units}


@router.get("/printers/{printer_id}/ams/current", tags=["AMS"])
async def get_ams_current(printer_id: int, db: Session = Depends(get_db)):
    """Get latest AMS environmental readings for a printer."""
    rows = db.execute(text("""
        SELECT ams_unit, humidity, temperature, recorded_at
        FROM ams_telemetry
        WHERE printer_id = :pid
        AND recorded_at = (
            SELECT MAX(recorded_at) FROM ams_telemetry t2
            WHERE t2.printer_id = ams_telemetry.printer_id
            AND t2.ams_unit = ams_telemetry.ams_unit
        )
        ORDER BY ams_unit
    """), {"pid": printer_id}).fetchall()

    units = []
    for row in rows:
        hum = row[1]
        hum_label = {1: "Dry", 2: "Low", 3: "Moderate", 4: "High", 5: "Wet"}.get(hum, "Unknown") if hum else "N/A"
        units.append({
            "unit": row[0], "humidity": hum, "humidity_label": hum_label,
            "temperature": row[2], "recorded_at": row[3],
        })

    return {"printer_id": printer_id, "units": units}


# ====================================================================
# AMS RFID Re-read & Slot Config (Bambu)
# ====================================================================

@router.post("/printers/{printer_id}/ams/refresh", tags=["AMS"])
async def refresh_ams_rfid(
    printer_id: int,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Trigger AMS RFID re-read on a Bambu printer."""
    from modules.printers.route_utils import _bambu_command_direct
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if printer.api_type != "bambu":
        raise HTTPException(status_code=400, detail="AMS refresh is only supported on Bambu printers")
    if _bambu_command_direct(printer, "refresh_ams_rfid"):
        return {"success": True, "message": "AMS RFID re-read triggered"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


@router.put("/printers/{printer_id}/ams/{ams_id}/slots/{slot_id}", tags=["AMS"])
async def configure_ams_slot(
    printer_id: int,
    ams_id: int,
    slot_id: int,
    body: dict,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Configure an AMS slot's filament settings (Bambu only).

    Body: {"material": "PETG", "color": "#FF5500", "k_factor": 0.028, "spool_id": 12}
    """
    from modules.printers.route_utils import _bambu_command_direct
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if printer.api_type != "bambu":
        raise HTTPException(status_code=400, detail="AMS slot config is only supported on Bambu printers")
    if ams_id < 0 or ams_id > 3 or slot_id < 0 or slot_id > 3:
        raise HTTPException(status_code=422, detail="ams_id and slot_id must be 0-3")

    material = body.get("material", "PLA")
    color = body.get("color", "#FFFFFF")
    k_factor = body.get("k_factor", 0.0)
    spool_id = body.get("spool_id")

    _bambu_command_direct(printer, "set_ams_filament", ams_id, slot_id, material, color, k_factor)

    if spool_id is not None:
        slot_number = ams_id * 4 + slot_id
        slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == printer_id,
            FilamentSlot.slot_number == slot_number,
        ).first()
        if slot:
            slot.filament_type = material
            slot.color = body.get("color", slot.color)
            slot.assigned_spool_id = spool_id
            db.commit()

    return {"success": True, "message": f"AMS {ams_id} slot {slot_id} configured"}
