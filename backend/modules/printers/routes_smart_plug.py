"""Smart plug routes — plug configuration, power control, energy monitoring."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role
import core.crypto as crypto
import modules.printers.smart_plug as smart_plug

log = logging.getLogger("odin.api")
router = APIRouter()


@router.get("/printers/{printer_id}/plug", tags=["Smart Plug"])
async def get_plug_config(printer_id: int, db: Session = Depends(get_db)):
    """Get smart plug configuration for a printer."""
    result = db.execute(text("""
        SELECT plug_type, plug_host, plug_entity_id, plug_auto_on, plug_auto_off,
               plug_cooldown_minutes, plug_power_state, plug_energy_kwh
        FROM printers WHERE id = :id
    """), {"id": printer_id}).fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="Printer not found")

    return {
        "type": result[0],
        "host": result[1],
        "entity_id": result[2],
        "auto_on": bool(result[3]) if result[3] is not None else True,
        "auto_off": bool(result[4]) if result[4] is not None else True,
        "cooldown_minutes": result[5] or 5,
        "power_state": result[6],
        "energy_kwh": result[7] or 0,
        "configured": result[0] is not None,
    }


@router.put("/printers/{printer_id}/plug", tags=["Smart Plug"])
async def update_plug_config(printer_id: int, request: Request, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update smart plug configuration for a printer."""
    data = await request.json()

    plug_type = data.get("type")
    if plug_type and plug_type not in ("tasmota", "homeassistant", "mqtt"):
        raise HTTPException(400, "Invalid plug type. Use: tasmota, homeassistant, mqtt")

    db.execute(text("""
        UPDATE printers SET
            plug_type = :plug_type,
            plug_host = :plug_host,
            plug_entity_id = :plug_entity_id,
            plug_auth_token = :plug_auth_token,
            plug_auto_on = :plug_auto_on,
            plug_auto_off = :plug_auto_off,
            plug_cooldown_minutes = :plug_cooldown_minutes
        WHERE id = :id
    """), {
        "id": printer_id,
        "plug_type": plug_type,
        "plug_host": data.get("host"),
        "plug_entity_id": data.get("entity_id"),
        "plug_auth_token": crypto.encrypt(data.get("auth_token")) if data.get("auth_token") else None,
        "plug_auto_on": data.get("auto_on", True),
        "plug_auto_off": data.get("auto_off", True),
        "plug_cooldown_minutes": data.get("cooldown_minutes", 5),
    })
    db.commit()

    return {"status": "ok", "message": "Smart plug configuration updated"}


@router.delete("/printers/{printer_id}/plug", tags=["Smart Plug"])
async def remove_plug_config(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Remove smart plug configuration from a printer."""
    db.execute(text("""
        UPDATE printers SET
            plug_type = NULL, plug_host = NULL, plug_entity_id = NULL,
            plug_auth_token = NULL, plug_auto_on = 1, plug_auto_off = 1,
            plug_cooldown_minutes = 5, plug_power_state = NULL
        WHERE id = :id
    """), {"id": printer_id})
    db.commit()
    return {"status": "ok"}


@router.post("/printers/{printer_id}/plug/on", tags=["Smart Plug"])
async def plug_power_on(printer_id: int, current_user: dict = Depends(require_role("operator"))):
    """Turn on a printer's smart plug."""
    result = smart_plug.power_on(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@router.post("/printers/{printer_id}/plug/off", tags=["Smart Plug"])
async def plug_power_off(printer_id: int, current_user: dict = Depends(require_role("operator"))):
    """Turn off a printer's smart plug."""
    result = smart_plug.power_off(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@router.post("/printers/{printer_id}/plug/toggle", tags=["Smart Plug"])
async def plug_power_toggle(printer_id: int, current_user: dict = Depends(require_role("operator"))):
    """Toggle a printer's smart plug."""
    result = smart_plug.power_toggle(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@router.get("/printers/{printer_id}/plug/energy", tags=["Smart Plug"])
async def plug_energy(printer_id: int):
    """Get current energy data from smart plug."""
    data = smart_plug.get_energy(printer_id)
    if data is None:
        raise HTTPException(400, "No energy data available")
    return data


@router.get("/printers/{printer_id}/plug/state", tags=["Smart Plug"])
async def plug_state(printer_id: int):
    """Query current power state from smart plug."""
    state = smart_plug.get_power_state(printer_id)
    if state is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": state}


@router.get("/settings/energy-rate", tags=["Smart Plug"])
async def get_energy_rate(db: Session = Depends(get_db)):
    """Get energy cost per kWh."""
    result = db.execute(text("SELECT value FROM system_config WHERE key = 'energy_cost_per_kwh'")).fetchone()
    return {"energy_cost_per_kwh": float(result[0]) if result else 0.12}


@router.put("/settings/energy-rate", tags=["Smart Plug"])
async def set_energy_rate(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set energy cost per kWh."""
    data = await request.json()
    rate = data.get("energy_cost_per_kwh", 0.12)
    db.execute(text(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES ('energy_cost_per_kwh', :rate)"
    ), {"rate": str(rate)})
    db.commit()
    return {"energy_cost_per_kwh": rate}
