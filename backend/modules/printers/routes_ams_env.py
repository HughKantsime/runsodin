"""AMS environment routes — AMS telemetry, current readings, RFID refresh, slot configuration."""
from core.base import FilamentType, slot_filament_value

import logging
import re
from modules.inventory.services import assign_slot
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import sql
from core.rbac import require_role, check_org_access, require_action
from modules.printers.models import Printer, FilamentSlot
from modules.printers.ledger_display import ensure_local_filaments

log = logging.getLogger("odin.api")
router = APIRouter()


def _ams_printer(printer_id: int, db: Session, current_user: dict, *, write: bool = False):
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not (check_org_access(current_user, printer.org_id) or (not write and printer.shared)):
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer


# ====================================================================
# AMS Environment
# ====================================================================

@router.get("/printers/{printer_id}/ams/environment", tags=["AMS"])
async def get_ams_environment(
    printer_id: int,
    hours: int = Query(default=24, ge=1, le=168),
    unit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer", scope="read:printers")),
):
    """Get AMS humidity/temperature history for charts."""
    printer = _ams_printer(printer_id, db, current_user)
    _cutoff = sql.now_offset(f"-{hours} hours")
    query = f"""
        SELECT ams_unit, humidity, temperature, recorded_at
        FROM ams_telemetry
        WHERE printer_id = :printer_id
        AND recorded_at >= {_cutoff}
    """
    params = {"printer_id": printer_id}

    if unit is not None:
        query += " AND ams_unit = :unit"
        params["unit"] = unit

    query += " ORDER BY recorded_at ASC"

    rows = db.execute(text(query), params).fetchall()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)

    units = {}
    for row in rows:
        u = row[0]
        if u not in units:
            units[u] = []
        units[u].append({"humidity": row[1], "temperature": row[2], "time": row[3]})

    return {"printer_id": printer_id, "hours": hours, "units": units,
            "humidity_unit": "level" if printer.api_type == "bambu" else "unknown"}


@router.get("/printers/{printer_id}/ams/current", tags=["AMS"])
async def get_ams_current(printer_id: int, db: Session = Depends(get_db),
                          current_user: dict = Depends(require_role("viewer", scope="read:printers"))):
    """Get latest AMS environmental readings for a printer."""
    _ams_printer(printer_id, db, current_user)
    rows = db.execute(text("""
        SELECT ams_unit, humidity, temperature, recorded_at
        FROM (
            SELECT ams_unit, humidity, temperature, recorded_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY ams_unit ORDER BY recorded_at DESC, id DESC
                   ) AS reading_rank
            FROM ams_telemetry
            WHERE printer_id = :pid AND recorded_at IS NOT NULL
        ) AS latest
        WHERE reading_rank = 1
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
    current_user: dict = Depends(require_action("printers.edit", scope="write:printers")),
    db: Session = Depends(get_db),
):
    """Trigger AMS RFID re-read on a Bambu printer."""
    from modules.printers.route_utils import _bambu_command_direct
    printer = _ams_printer(printer_id, db, current_user, write=True)
    if printer.api_type != "bambu":
        raise HTTPException(status_code=400, detail="AMS refresh is only supported on Bambu printers")
    if _bambu_command_direct(printer, "refresh_ams_rfid"):
        return {"success": True, "message": "AMS RFID re-read triggered"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


class AMSSlotConfigRequest(PydanticBaseModel):
    # Validate after authorization, returning the route's explicit 400 contract.
    preset_id: object = None
    material: object = None
    color: object = None
    nozzle_temp_min: object = None
    nozzle_temp_max: object = None
    spool_id: Optional[int] = None


def validate_tray_settings(body):
    if body is None:
        raise HTTPException(400, 'Explicit tray settings are required')
    if (not isinstance(body.preset_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", body.preset_id)
            or not isinstance(body.material, str) or not 1 <= len(body.material.strip()) <= 64
            or not isinstance(body.color, str) or not re.fullmatch(r"#?[0-9A-Fa-f]{6}(?:[Ff]{2})?", body.color)
            or type(body.nozzle_temp_min) is not int or type(body.nozzle_temp_max) is not int
            or not 0 <= body.nozzle_temp_min <= body.nozzle_temp_max <= 350):
        raise HTTPException(400, 'Supply a preset id, material, RGB colour and nozzle temperature range (0–350 C)')
    body.material = body.material.strip()
    return body


@router.get("/printers/{printer_id}/ams/presets", tags=["AMS"])
def reported_tray_presets(printer_id: int, db: Session = Depends(get_db),
                         current_user: dict = Depends(require_role("viewer", scope="read:printers"))):
    """Only caller-visible reports with complete temperatures; no guessed profiles."""
    import json
    from modules.printers.telemetry.validators import coerce_int_or_raise
    _ams_printer(printer_id, db, current_user)
    found = {}
    for slot in db.query(FilamentSlot).filter(FilamentSlot.printer_id == printer_id).all():
        try:
            report = json.loads(slot.tray_observation or '{}').get('reported_preset', {})
            body = validate_tray_settings(AMSSlotConfigRequest(
                preset_id=report.get('tray_info_idx'), material=report.get('tray_type'), color='FFFFFF',
                nozzle_temp_min=coerce_int_or_raise(report['nozzle_temp_min']), nozzle_temp_max=coerce_int_or_raise(report['nozzle_temp_max'])))
            item = body.model_dump(exclude={'color', 'spool_id'})
            found[tuple(item.values())] = item
        except (ValueError, TypeError, KeyError, AttributeError, HTTPException):
            continue
    return list(found.values())


@router.put("/printers/{printer_id}/ams/{ams_id}/slots/{slot_id}", tags=["AMS"])
async def configure_ams_slot(
    printer_id: int,
    ams_id: int,
    slot_id: int,
    body: Optional[AMSSlotConfigRequest] = None,
    current_user: dict = Depends(require_action("printers.edit", scope="write:printers")),
    db: Session = Depends(get_db),
):
    """Configure an AMS slot's filament settings (Bambu only).

    Requires explicit preset, material, colour and temperature limits.
    """
    from core.dependencies import log_audit
    from modules.printers.telemetry.bambu.ams_configuration import load_ams_configuration, AMSConfigurationUnavailable
    from modules.printers.telemetry.bambu.tray_setting import set_tray
    from starlette.concurrency import run_in_threadpool
    printer = _ams_printer(printer_id, db, current_user, write=True)
    ensure_local_filaments(printer_id)
    if printer.api_type != "bambu":
        raise HTTPException(status_code=400, detail="AMS slot config is only supported on Bambu printers")
    if ams_id < 0 or ams_id > 3 or slot_id < 0 or slot_id > 3:
        raise HTTPException(status_code=400, detail="ams_id and slot_id must be 0-3")

    validate_tray_settings(body)
    material = body.material
    color = body.color
    spool_id = body.spool_id

    slot = None
    if spool_id is not None:
        from modules.inventory.models import Spool
        spool = db.query(Spool).filter(Spool.id == spool_id).first()
        if not spool or not check_org_access(current_user, spool.org_id):
            raise HTTPException(status_code=404, detail="Spool not found")
        slot_number = ams_id * 4 + slot_id + 1
        slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == printer_id,
            FilamentSlot.slot_number == slot_number,
        ).first()
        if not slot:
            raise HTTPException(status_code=404, detail="Filament slot not found")

    try:
        config = load_ams_configuration(db, printer_id).transport
    except AMSConfigurationUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    reply = await run_in_threadpool(set_tray, config, ams_id, slot_id, body.preset_id,
                                   material, color, body.nozzle_temp_min, body.nozzle_temp_max)
    accepted = str(reply.get('result')).lower() == 'success'
    log_audit(db, 'printer.tray_set_requested', 'printer', printer_id,
              {'printer_id': printer_id, 'ams_id': ams_id, 'slot_id': slot_id,
               'preset_id': body.preset_id, 'outcome': reply})
    if not accepted:
        db.commit()
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503 if reply.get('result') is None else 409,
                            content={'success': False, **reply, 'detail': {'message': 'Tray setting not confirmed', **reply}})

    if slot is not None:
        slot.filament_type = FilamentType(slot_filament_value(material))
        slot.color = body.color
        assign_slot(db, slot, spool_id, current_user)
    db.commit()
    return {"success": True, **reply}
