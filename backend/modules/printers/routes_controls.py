"""Printer control routes — stop, pause, resume, lights, speed, fans, clear-errors, skip-objects, plate-cleared."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role
import core.crypto as crypto
from modules.printers.models import Printer
from modules.printers.route_utils import _send_printer_command, _bambu_command_direct

log = logging.getLogger("odin.api")
router = APIRouter()


# ====================================================================
# Printer Commands (Stop / Pause / Resume)
# ====================================================================

@router.post("/printers/{printer_id}/stop", tags=["Printers"])
async def stop_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Emergency stop - cancel current print."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    action = "cancel_print" if printer.api_type == "moonraker" else "stop_print"
    if _send_printer_command(printer, action):
        db.execute(text("UPDATE printers SET gcode_state = 'IDLE', print_stage = 'Idle' WHERE id = :id"), {"id": printer_id})
        db.commit()
        return {"success": True, "message": "Print stopped"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


@router.post("/printers/{printer_id}/pause", tags=["Printers"])
async def pause_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Pause current print."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if _send_printer_command(printer, "pause_print"):
        db.execute(text("UPDATE printers SET gcode_state = 'PAUSED' WHERE id = :id"), {"id": printer_id})
        db.commit()
        return {"success": True, "message": "Print paused"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


@router.post("/printers/{printer_id}/resume", tags=["Printers"])
async def resume_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Resume paused print."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if _send_printer_command(printer, "resume_print"):
        db.execute(text("UPDATE printers SET gcode_state = 'RUNNING' WHERE id = :id"), {"id": printer_id})
        db.commit()
        return {"success": True, "message": "Print resumed"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


# ====================================================================
# Bambu-specific printer controls
# ====================================================================

@router.post("/printers/{printer_id}/clear-errors", tags=["Printers"])
async def clear_printer_errors(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Clear HMS/print errors on a Bambu printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if printer.api_type != "bambu":
        raise HTTPException(status_code=400, detail="Clear errors is only supported on Bambu printers")
    if _bambu_command_direct(printer, "clear_print_errors"):
        return {"success": True, "message": "Error clear command sent"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


@router.post("/printers/{printer_id}/skip-objects", tags=["Printers"])
async def skip_printer_objects(
    printer_id: int,
    body: dict,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Skip objects during an active Bambu print. Body: {"object_ids": [0, 1]}"""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if printer.api_type != "bambu":
        raise HTTPException(status_code=400, detail="Skip objects is only supported on Bambu printers")
    object_ids = body.get("object_ids", [])
    if not object_ids or not isinstance(object_ids, list):
        raise HTTPException(status_code=422, detail="object_ids must be a non-empty list of integers")
    if _bambu_command_direct(printer, "skip_objects", object_ids):
        return {"success": True, "message": f"Skip command sent for objects {object_ids}"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


@router.post("/printers/{printer_id}/speed", tags=["Printers"])
async def set_printer_speed(
    printer_id: int,
    body: dict,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Set print speed on a Bambu printer. Body: {"speed": 2} (1=Silent, 2=Standard, 3=Sport, 4=Ludicrous)"""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if printer.api_type != "bambu":
        raise HTTPException(status_code=400, detail="Speed control is only supported on Bambu printers")
    speed = body.get("speed")
    if speed not in (1, 2, 3, 4):
        raise HTTPException(status_code=422, detail="speed must be 1 (Silent), 2 (Standard), 3 (Sport), or 4 (Ludicrous)")
    if _bambu_command_direct(printer, "set_print_speed", speed):
        speed_names = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}
        return {"success": True, "message": f"Speed set to {speed_names[speed]}"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


# ====================================================================
# Fan Speed Controls (Bambu)
# ====================================================================

@router.post("/printers/{printer_id}/fan", tags=["Printers"])
async def set_fan_speed(
    printer_id: int,
    body: dict,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Set fan speed on a Bambu printer.

    Body: {"fan": "part_cooling"|"auxiliary"|"chamber", "speed": 0-255}
    """
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if printer.api_type != "bambu":
        raise HTTPException(status_code=400, detail="Fan control is only supported on Bambu printers")
    fan = body.get("fan")
    if fan not in ("part_cooling", "auxiliary", "chamber"):
        raise HTTPException(status_code=422, detail="fan must be 'part_cooling', 'auxiliary', or 'chamber'")
    speed = body.get("speed")
    if not isinstance(speed, int) or speed < 0 or speed > 255:
        raise HTTPException(status_code=422, detail="speed must be integer 0-255")
    if _bambu_command_direct(printer, "set_fan_speed", fan, speed):
        return {"success": True, "message": f"{fan} fan set to {speed}/255"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


# ====================================================================
# Lights (Bambu)
# ====================================================================

@router.post("/printers/{printer_id}/lights", tags=["Printers"])
def toggle_printer_lights(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Toggle chamber lights on/off for a Bambu printer."""
    from datetime import datetime, timezone
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if not printer.api_type or printer.api_type.lower() != "bambu":
        raise HTTPException(status_code=400, detail="Light control only supported for Bambu printers")
    if not printer.api_host or not printer.api_key:
        raise HTTPException(status_code=400, detail="Printer connection not configured")

    decrypted_key = crypto.decrypt(printer.api_key)
    if "|" not in decrypted_key:
        raise HTTPException(status_code=400, detail="Invalid api_key format")

    serial, access_code = decrypted_key.split("|", 1)
    turn_on = not printer.lights_on

    try:
        from modules.printers.adapters.bambu import BambuPrinter

        bambu = BambuPrinter(ip=printer.api_host, serial=serial, access_code=access_code)
        if not bambu.connect():
            raise HTTPException(status_code=503, detail="Failed to connect to printer")

        time.sleep(3)

        payload = {
            'system': {
                'sequence_id': '0',
                'command': 'ledctrl',
                'led_node': 'chamber_light',
                'led_mode': 'on' if turn_on else 'off',
            }
        }
        success = bambu._publish(payload)
        time.sleep(1)
        bambu.disconnect()

        if not success:
            raise HTTPException(status_code=503, detail="Failed to send light command")

        printer.lights_on = turn_on
        printer.lights_toggled_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(printer)

        return {"lights_on": turn_on, "message": f"Lights {'on' if turn_on else 'off'}"}

    except ImportError:
        raise HTTPException(status_code=500, detail="bambu_adapter not installed")
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Printer connection error (lights control): {e}")
        raise HTTPException(status_code=503, detail="Printer connection error. Check printer IP and credentials.")


# ====================================================================
# Clear Plate Confirmation
# ====================================================================

@router.post("/printers/{printer_id}/plate-cleared", tags=["Printers"])
async def plate_cleared(
    printer_id: int,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Confirm plate is cleared, allowing the next queued job to dispatch."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    waiting = db.execute(
        text("""
            SELECT id FROM jobs
            WHERE printer_id = :pid AND status = 'waiting_plate_clear'
            ORDER BY queue_position ASC, id ASC LIMIT 1
        """),
        {"pid": printer_id},
    ).fetchone()

    if not waiting:
        return {"success": True, "message": "No jobs waiting for plate clear"}

    db.execute(
        text("UPDATE jobs SET status = 'pending' WHERE id = :jid"),
        {"jid": waiting[0]},
    )
    db.commit()
    return {"success": True, "message": f"Job {waiting[0]} released for dispatch"}
