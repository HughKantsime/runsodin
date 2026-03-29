"""
Printer health and telemetry helpers.

Provides update_telemetry(), mark_online(), mark_offline(), discover_camera(),
increment_care_counters(), increment_nozzle_lifecycle(), reset_maintenance_counters().
Called by all monitor daemons on state updates.
"""

import logging
from typing import Optional

from core.db_utils import get_db

log = logging.getLogger("printer_events")


def update_telemetry(
    printer_id: int,
    bed_temp: float = None,
    bed_target: float = None,
    nozzle_temp: float = None,
    nozzle_target: float = None,
    state: str = None,
    stage: str = None,
    progress_percent: int = None,
    remaining_minutes: int = None,
    current_layer: int = None,
    total_layers: int = None,
    # Brand-specific (optional)
    lights_on: bool = None,
    nozzle_type: str = None,
    nozzle_diameter: float = None,
    hms_errors: str = None,
):
    """
    Update printer telemetry in database.
    Called by all monitors on each status update.
    Only updates fields that are provided (not None).
    """
    updates = ["last_seen = datetime('now')"]
    params = []

    field_map = {
        "bed_temp": bed_temp,
        "bed_target_temp": bed_target,
        "nozzle_temp": nozzle_temp,
        "nozzle_target_temp": nozzle_target,
        "gcode_state": state,
        "print_stage": stage,
        "lights_on": lights_on,
        "nozzle_type": nozzle_type,
        "nozzle_diameter": nozzle_diameter,
        "hms_errors": hms_errors,
    }

    for col, val in field_map.items():
        if val is not None:
            updates.append(f"{col} = ?")
            params.append(val)

    params.append(printer_id)

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE printers SET {', '.join(updates)} WHERE id = ?",
                params
            )
            conn.commit()
    except Exception as e:
        log.error(f"Failed to update telemetry for printer {printer_id}: {e}")


def mark_online(printer_id: int):
    """Mark printer as online (update last_seen)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE printers SET last_seen = datetime('now') WHERE id = ?",
                (printer_id,)
            )
            conn.commit()
    except Exception as e:
        log.error(f"Failed to mark printer {printer_id} online: {e}")


def mark_offline(printer_id: int):
    """Mark printer as offline (clear state)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE printers SET
                    gcode_state = 'offline',
                    print_stage = NULL
                WHERE id = ?""",
                (printer_id,)
            )
            conn.commit()
    except Exception as e:
        log.error(f"Failed to mark printer {printer_id} offline: {e}")


def discover_camera(printer_id: int, rtsp_url: str):
    """
    Trigger go2rtc config sync when a Bambu camera is discovered via MQTT.

    We do NOT persist the RTSP URL to camera_url — the URL contains plaintext
    credentials (bblp access_code) that are already stored encrypted in api_key.
    get_camera_url() regenerates the URL on-demand from the encrypted credentials.
    """
    try:
        # Regenerate go2rtc config so the camera stream is available
        try:
            from main import sync_go2rtc_config_standalone
            sync_go2rtc_config_standalone()
            log.info(f"go2rtc config synced after camera discovery for printer {printer_id}")
        except Exception as e2:
            log.warning(f"Could not sync go2rtc config: {e2}")
    except Exception as e:
        log.error(f"Failed to sync go2rtc for printer {printer_id}: {e}")


def increment_care_counters(printer_id: int, print_hours: float, print_count: int = 1):
    """
    Increment care counters after job completion.
    Called by all monitors when a print finishes successfully.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE printers SET
                    total_print_hours = COALESCE(total_print_hours, 0) + ?,
                    total_print_count = COALESCE(total_print_count, 0) + ?,
                    hours_since_maintenance = COALESCE(hours_since_maintenance, 0) + ?,
                    prints_since_maintenance = COALESCE(prints_since_maintenance, 0) + ?
                WHERE id = ?""",
                (print_hours, print_count, print_hours, print_count, printer_id)
            )
            conn.commit()
        log.debug(f"Incremented care counters for printer {printer_id}: +{print_hours:.2f}h, +{print_count} prints")
        # Also increment nozzle lifecycle counters
        increment_nozzle_lifecycle(printer_id, print_hours, print_count)
    except Exception as e:
        log.error(f"Failed to increment care counters for printer {printer_id}: {e}")


def increment_nozzle_lifecycle(printer_id: int, print_hours: float, print_count: int = 1):
    """Increment the current (active) nozzle's usage counters after job completion."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE nozzle_lifecycle SET
                    print_hours_accumulated = print_hours_accumulated + ?,
                    print_count = print_count + ?
                WHERE printer_id = ? AND removed_at IS NULL""",
                (print_hours, print_count, printer_id)
            )
            conn.commit()
    except Exception as e:
        log.debug(f"Nozzle lifecycle update for printer {printer_id}: {e}")


def reset_maintenance_counters(printer_id: int):
    """
    Reset maintenance counters after maintenance is performed.
    Called from maintenance API endpoint.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE printers SET
                    hours_since_maintenance = 0,
                    prints_since_maintenance = 0
                WHERE id = ?""",
                (printer_id,)
            )
            conn.commit()
        log.info(f"Reset maintenance counters for printer {printer_id}")
    except Exception as e:
        log.error(f"Failed to reset maintenance counters for printer {printer_id}: {e}")
