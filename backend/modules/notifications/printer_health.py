"""
Printer health and telemetry helpers.

Provides update_telemetry(), mark_online(), mark_offline(), discover_camera(),
increment_care_counters(), increment_nozzle_lifecycle(), reset_maintenance_counters().
Called by all monitor daemons on state updates.
"""

import logging
from typing import Optional

# Resolved at call time: never a stale get_db bound at import.
from core.db_utils import live_get_db as get_db
from core.db_compat import sql

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
    updates = [f"last_seen = {sql.now()}"]
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
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
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
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
                f"UPDATE printers SET last_seen = {sql.now()} WHERE id = ?",
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
    """Register an approved local discovery source, then use existing config sync.

    Bambu reports are presence signals only. Other protocols may discover HTTP
    camera endpoints on their configured printer host; cross-host cameras need
    explicit configuration. Sources are encrypted before storage.
    """
    try:
        from core import crypto
        from modules.printers.route_utils import (
            _check_ssrf_blocklist, _CAMERA_SHELL_METACHAR_RE,
            sync_go2rtc_config_raw,
        )
        from urllib.parse import urlsplit

        with get_db() as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT api_type, api_host, camera_url FROM printers "
                "WHERE id = ? AND is_active IS TRUE AND camera_enabled IS TRUE",
                (printer_id,),
            ).fetchone()
            if not row:
                return
            protocol, configured_host, existing_source = row
            if protocol != "bambu":
                if existing_source:
                    return
                if protocol not in {"moonraker", "prusalink", "elegoo"}:
                    return
                if not isinstance(rtsp_url, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in rtsp_url):
                    return
                if _CAMERA_SHELL_METACHAR_RE.search(rtsp_url):
                    return
                source = urlsplit(rtsp_url)
                host = source.hostname
                # Accessing port validates malformed/out-of-range authorities.
                port = source.port
                configured = urlsplit("//" + (configured_host or ""))
                configured_port = configured.port
                if (source.scheme not in {"http", "https"} or not host
                        or source.username is not None or source.password is not None
                        or source.fragment or "#" in rtsp_url
                        or host.lower().rstrip(".") != (configured.hostname or "").lower().rstrip(".")):
                    return
                _check_ssrf_blocklist(host)
                encrypted = crypto.encrypt(rtsp_url)
                cur.execute(
                    "UPDATE printers SET camera_url = ? WHERE id = ? "
                    "AND api_type = ? AND api_host = ? "
                    "AND is_active IS TRUE AND camera_enabled IS TRUE "
                    "AND (camera_url IS NULL OR camera_url = '')",
                    (encrypted, printer_id, protocol, configured_host),
                )
                if cur.rowcount != 1:
                    return
                conn.commit()
        sync_go2rtc_config_raw()
        log.info("go2rtc config synced after camera discovery for printer %s", printer_id)
    except Exception:
        # Transport/config exceptions can contain credential-bearing URLs.
        log.warning("Camera discovery registration failed for printer %s", printer_id)


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
