"""Printer status routes — live status, telemetry, HMS error history, nozzle status."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import sql
from core.rbac import require_role
import core.crypto as crypto
from modules.printers.models import Printer

log = logging.getLogger("odin.api")
router = APIRouter()


# ====================================================================
# Live Status
# ====================================================================

def _fetch_printer_live_status(printer_id: int, db: Session) -> dict:
    """Shared logic: fetch real-time status from a single printer via MQTT."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if not printer.api_host or not printer.api_key:
        return {"error": "Printer not configured for MQTT"}

    try:
        parts = crypto.decrypt(printer.api_key).split("|")
        if len(parts) != 2:
            return {"error": "Invalid credentials format"}
        serial, access_code = parts
    except Exception:
        return {"error": "Could not decrypt credentials"}

    # Route through V2 when ODIN_TELEMETRY_V2=1; legacy path otherwise.
    from modules.printers.telemetry.feature_flag import is_v2_enabled
    if is_v2_enabled():
        return _fetch_live_status_v2(printer_id, printer, serial, access_code)
    return _fetch_live_status_legacy(printer_id, printer, serial, access_code)


def _fetch_live_status_v2(printer_id, printer, serial, access_code) -> dict:
    """V2 path — uses BambuTelemetryAdapter via read_status_once."""
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.session import read_status_once

    config = BambuAdapterConfig(
        printer_id=f"legacy-route-{printer.id}",
        serial=serial,
        host=printer.api_host,
        access_code=access_code,
    )
    try:
        result = read_status_once(config, timeout=5.0)
    except Exception as e:
        log.debug("V2 live status fetch error for printer %s: %s", printer_id, e, exc_info=True)
        return {"error": "Unable to fetch live status"}

    if not result.success:
        return {"error": result.error or "Connection failed"}
    if result.section is None:
        return {"error": "Timeout waiting for status"}

    s = result.section
    return {
        "printer_id": printer_id,
        "printer_name": printer.name,
        "gcode_state": s.gcode_state.value if s.gcode_state else None,
        "job_name": s.subtask_name,
        "progress": s.mc_percent,
        "layer": s.layer_num,
        "total_layers": s.total_layer_num,
        "time_remaining": s.mc_remaining_time,
        "bed_temp": s.bed_temper,
        "bed_target": s.bed_target_temper,
        "nozzle_temp": s.nozzle_temper,
        "nozzle_target": s.nozzle_target_temper,
        "wifi_signal": s.wifi_signal,
    }


def _fetch_live_status_legacy(printer_id, printer, serial, access_code) -> dict:
    """Legacy path — unchanged behavior for ODIN_TELEMETRY_V2=0."""
    from modules.printers.adapters.bambu import BambuPrinter

    status_data = {}
    def on_status(s):
        nonlocal status_data
        status_data = s.raw_data.get('print', {})

    try:
        bp = BambuPrinter(
            ip=printer.api_host,
            serial=serial,
            access_code=access_code,
            on_status_update=on_status,
        )
        if bp.connect():
            timeout = 5
            start = time.time()
            while not status_data and (time.time() - start) < timeout:
                time.sleep(0.2)
            bp.disconnect()

            if status_data:
                return {
                    "printer_id": printer_id,
                    "printer_name": printer.name,
                    "gcode_state": status_data.get('gcode_state'),
                    "job_name": status_data.get('subtask_name'),
                    "progress": status_data.get('mc_percent'),
                    "layer": status_data.get('layer_num'),
                    "total_layers": status_data.get('total_layer_num'),
                    "time_remaining": status_data.get('mc_remaining_time'),
                    "bed_temp": status_data.get('bed_temper'),
                    "bed_target": status_data.get('bed_target_temper'),
                    "nozzle_temp": status_data.get('nozzle_temper'),
                    "nozzle_target": status_data.get('nozzle_target_temper'),
                    "wifi_signal": status_data.get('wifi_signal'),
                }
            else:
                return {"error": "Timeout waiting for status"}
        else:
            return {"error": "Connection failed"}
    except Exception as e:
        log.debug("Live status fetch error for printer %s: %s", printer_id, e, exc_info=True)
        return {"error": "Unable to fetch live status"}


@router.get("/printers/{printer_id}/live-status", tags=["Printers"])
def get_printer_live_status(printer_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """Get real-time status from printer via MQTT."""
    return _fetch_printer_live_status(printer_id, db)


# ====================================================================
# Telemetry
# ====================================================================

@router.get("/printers/{printer_id}/telemetry", tags=["Telemetry"])
def get_printer_telemetry(printer_id: int, hours: int = Query(24, ge=1, le=168),
                          current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get timeseries telemetry data for a printer (recorded during prints)."""
    cutoff_expr = sql.now_offset(f"-{hours} hours")
    rows = db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        f"SELECT recorded_at, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed "
        f"FROM printer_telemetry WHERE printer_id = :pid AND recorded_at > {cutoff_expr} "
        f"ORDER BY recorded_at ASC"
    ), {"pid": printer_id}).fetchall()
    return [{"recorded_at": r[0], "bed_temp": r[1], "nozzle_temp": r[2],
             "bed_target": r[3], "nozzle_target": r[4], "fan_speed": r[5]} for r in rows]


# ====================================================================
# HMS Error History
# ====================================================================

@router.get("/printers/{printer_id}/hms-history", tags=["Telemetry"])
def get_hms_error_history(printer_id: int, days: int = Query(30, ge=1, le=90),
                          current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get HMS error history with occurrence timestamps."""
    cutoff_expr = sql.now_offset(f"-{days} days")
    rows = db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        f"SELECT id, printer_id, code, message, severity, source, occurred_at "
        f"FROM hms_error_history WHERE printer_id = :pid AND occurred_at > {cutoff_expr} "
        f"ORDER BY occurred_at DESC"
    ), {"pid": printer_id}).fetchall()
    entries = [{"id": r[0], "printer_id": r[1], "code": r[2], "message": r[3],
                "severity": r[4], "source": r[5], "occurred_at": r[6]} for r in rows]
    freq = {}
    for e in entries:
        key = e["code"]
        freq[key] = freq.get(key, 0) + 1
    return {"entries": entries, "frequency": freq, "total": len(entries)}


# ====================================================================
# Nozzle Status (H2D dual-nozzle aware)
# ====================================================================

@router.get("/printers/{printer_id}/nozzle-status", tags=["Printers"])
def get_nozzle_status(printer_id: int, current_user: dict = Depends(require_role("viewer")),
                      db: Session = Depends(get_db)):
    """Get live nozzle temperature status. Returns dual nozzle data for H2D printers."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    nozzle_0 = {
        "temp": printer.nozzle_temp,
        "target": printer.nozzle_target_temp,
        "type": printer.nozzle_type,
        "diameter": printer.nozzle_diameter,
    }

    if printer.machine_type == "H2D":
        nozzle_1_temp = None
        nozzle_1_target = None
        try:
            pass
        except Exception as e:
            log.debug(f"Failed to get H2D nozzle 1 temps: {e}")

        return {
            "nozzle_count": 2,
            "nozzle_0": nozzle_0,
            "nozzle_1": {
                "temp": nozzle_1_temp,
                "target": nozzle_1_target,
                "type": printer.nozzle_type,
                "diameter": printer.nozzle_diameter,
            },
        }

    return {
        "nozzle_count": 1,
        "nozzle_0": nozzle_0,
        "nozzle_1": None,
    }
