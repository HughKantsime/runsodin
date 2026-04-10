"""
Moonraker Printer Monitor

Polls Moonraker REST API for printer status, similar to how
PrinterMonitor subscribes to Bambu MQTT. Designed to plug into
the existing MQTTMonitorDaemon alongside Bambu monitors.

Handles:
- Periodic status polling (every 3 seconds)
- Print job start/end detection and DB logging
- Progress tracking (percent, layers)
- Fan speed, nozzle diameter, speed/flow factors
- Timeseries telemetry (printer_telemetry table, 60s inserts)
- Filament slot sync (ACE/MMU → filament_slots table)
- Environment telemetry (ACE/chamber temp → ams_telemetry table)
- Filament runout detection and alerts
- Klipper error capture (hms_error_history table)
- Reconnection on failure
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from sqlalchemy import text

from modules.printers.adapters.moonraker import MoonrakerPrinter, MoonrakerState
from core.db import engine
from core.db_compat import sql

# WebSocket push (same as mqtt_monitor)
try:
    from core.ws_hub import push_event as ws_push
except ImportError:
    def ws_push(*a, **kw): pass

# MQTT republish (same as mqtt_monitor)
try:
    import modules.notifications.mqtt_republish as mqtt_republish
except ImportError:
    mqtt_republish = None

log = logging.getLogger("moonraker_monitor")

POLL_INTERVAL = 3          # seconds between status polls
RECONNECT_INTERVAL = 30    # seconds between reconnection attempts
PROGRESS_DB_INTERVAL = 5   # seconds between progress DB writes (throttle)
TELEMETRY_INSERT_INTERVAL = 60   # seconds between timeseries inserts
SLOT_SYNC_INTERVAL = 60         # seconds between filament slot syncs
ENV_TELEMETRY_INTERVAL = 300    # seconds between environment telemetry inserts (5 min)

# Material string → FilamentType enum value mapping (matches Bambu sync in main.py)
_MATERIAL_MAP = {
    "PLA": "PLA", "PETG": "PETG", "ABS": "ABS", "ASA": "ASA",
    "TPU": "TPU", "PA": "PA", "PC": "PC", "PVA": "PVA",
    "PLA-S": "PLA_SUPPORT", "PA-S": "PLA_SUPPORT", "PETG-S": "PLA_SUPPORT",
    "PA-CF": "NYLON_CF", "PA-GF": "NYLON_GF", "PET-CF": "PETG_CF", "PLA-CF": "PLA_CF",
}


class MoonrakerMonitor:
    """
    Monitors a single Moonraker printer via REST polling.
    
    Drop-in complement to the MQTT-based PrinterMonitor class.
    Same DB tables, same state tracking, same job logging.
    """
    
    def __init__(self, printer_id: int, name: str, host: str, port: int = 80, api_key: str = ""):
        self.printer_id = printer_id
        self.name = name
        self.host = host
        self.port = port
        
        self.printer = MoonrakerPrinter(host=host, port=port, api_key=api_key)
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._prev_state: Optional[str] = None
        self._current_job_db_id: Optional[int] = None
        self._last_progress_write = 0.0
        self._last_filename = ""
        self._last_telemetry_insert = 0.0
        self._last_slot_sync = 0.0
        self._last_env_telemetry = 0.0
        self._prev_filament_detected: Optional[bool] = None
    
    # ==================== Lifecycle ====================
    
    def connect(self) -> bool:
        """Connect to the printer and start polling thread."""
        if self.printer.connect():
            # Auto-discover camera URL and save to DB
            self._discover_and_save_camera()
            self._running = True
            self._thread = threading.Thread(
                target=self._poll_loop,
                name=f"moonraker-{self.name}",
                daemon=True,
            )
            self._thread.start()
            return True
        return False

    def _discover_and_save_camera(self):
        """Save discovered webcam URL to DB via printer_events."""
        import modules.notifications.event_dispatcher as printer_events
        try:
            urls = self.printer.get_webcam_urls()
            stream_url = urls.get("stream_url", "")
            if stream_url:
                printer_events.discover_camera(self.printer_id, stream_url)
                log.info(f"[{self.name}] Camera discovered: {stream_url}")
        except Exception as e:
            log.warning(f"[{self.name}] Camera discovery failed: {e}")
    
    def disconnect(self):
        """Stop polling and disconnect."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self.printer.disconnect()
        log.info(f"[{self.name}] Disconnected")
    
    # ==================== Poll Loop ====================
    
    def _poll_loop(self):
        """Main polling loop — runs in its own thread."""
        while self._running:
            try:
                status = self.printer.get_status()
                self._process_status(status)
            except Exception as e:
                log.error(f"[{self.name}] Poll error: {e}")
                # Try to reconnect
                self._handle_disconnect()
            
            time.sleep(POLL_INTERVAL)
    
    def _handle_disconnect(self):
        """Handle lost connection — attempt reconnection."""
        self.printer._connected = False
        log.warning(f"[{self.name}] Connection lost, retrying in {RECONNECT_INTERVAL}s")
        
        while self._running:
            time.sleep(RECONNECT_INTERVAL)
            try:
                if self.printer.connect():
                    self._discover_and_save_camera()
                    log.info(f"[{self.name}] Reconnected")
                    # Recover job tracking state after reconnect
                    self._recover_after_reconnect()
                    return
            except Exception as e:
                log.debug(f"Reconnect attempt failed: {e}")
            log.warning(f"[{self.name}] Reconnect failed, retrying...")
    
    def _recover_after_reconnect(self):
        """Restore job tracking state after a reconnect."""
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT id, job_name FROM print_jobs "
                    "WHERE printer_id = :pid AND status = 'running' ORDER BY id DESC LIMIT 1"),
                    {"pid": self.printer_id}).fetchone()
                if row:
                    self._current_job_db_id = row[0]
                    self._last_filename = row[1] or ""
                    log.info(f"[{self.name}] Recovered job tracking: {row[0]} ({row[1]})")
        except Exception as e:
            log.warning(f"[{self.name}] Job recovery after reconnect failed: {e}")

    # ==================== Status Processing ====================
    
    def _process_status(self, status):
        """Process a status update — detect state changes, track jobs."""
        import modules.notifications.event_dispatcher as printer_events
        now = time.time()

        # Update telemetry + heartbeat (throttled to every 10 seconds)
        if now - getattr(self, '_last_heartbeat', 0) >= 10:
            try:
              with engine.begin() as conn:
                bed_t = bed_tt = noz_t = noz_tt = None
                gstate = None
                stage = 'Idle'
                progress = None
                remaining_min = None
                current_layer = None
                total_layers = None
                fan_speed_val = status.fan_speed or None
                noz_dia = status.nozzle_diameter

                if hasattr(status, 'raw_data') and status.raw_data:
                    rd = status.raw_data
                    hb = rd.get('heater_bed', {})
                    ext = rd.get('extruder', {})
                    bed_t = hb.get('temperature')
                    bed_tt = hb.get('target')
                    noz_t = ext.get('temperature')
                    noz_tt = ext.get('target')
                    ps = rd.get('print_stats', {})
                    mk_state = ps.get('state', '')
                    gstate = mk_state.upper() if mk_state else None
                    if mk_state == 'printing': stage = 'Printing'
                    elif mk_state == 'paused': stage = 'Paused'
                    elif noz_tt and noz_tt > 0: stage = 'Heating hotend'
                    elif bed_tt and bed_tt > 0: stage = 'Heating bed'

                # Progress data
                progress = status.progress_percent
                current_layer = status.current_layer
                total_layers = status.total_layers

                # Calculate remaining time from elapsed + progress
                if status.print_duration and status.print_duration > 0 and progress and progress > 1:
                    elapsed_min = status.print_duration / 60.0
                    remaining_min = round(elapsed_min * (100.0 - progress) / progress)
                    if remaining_min < 0:
                        remaining_min = 0

                conn.execute(
                    text(f"UPDATE printers SET last_seen={sql.now()},"  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- safe: text() uses :param bindings; only sql.* helpers (constants) interpolated via f-string
                    " bed_temp=COALESCE(:bed_t,bed_temp),bed_target_temp=COALESCE(:bed_tt,bed_target_temp),"
                    " nozzle_temp=COALESCE(:noz_t,nozzle_temp),nozzle_target_temp=COALESCE(:noz_tt,nozzle_target_temp),"
                    " gcode_state=COALESCE(:gstate,gcode_state),print_stage=COALESCE(:stage,print_stage),"
                    " fan_speed=COALESCE(:fan_speed,fan_speed),nozzle_diameter=COALESCE(:noz_dia,nozzle_diameter)"
                    " WHERE id=:pid"),
                    {"bed_t": bed_t, "bed_tt": bed_tt, "noz_t": noz_t, "noz_tt": noz_tt,
                     "gstate": gstate, "stage": stage, "fan_speed": fan_speed_val,
                     "noz_dia": noz_dia, "pid": self.printer_id})

                # WebSocket push to frontend (same as Bambu monitor)
                ws_push('printer_telemetry', {
                    'printer_id': self.printer_id,
                    'bed_temp': bed_t,
                    'bed_target': bed_tt,
                    'nozzle_temp': noz_t,
                    'nozzle_target': noz_tt,
                    'state': gstate,
                    'progress': progress,
                    'remaining_min': remaining_min,
                    'current_layer': current_layer,
                    'total_layers': total_layers,
                    'fan_speed': fan_speed_val,
                })

                # MQTT republish to external broker (same as Bambu monitor)
                if mqtt_republish:
                    try:
                        mqtt_republish.republish_telemetry(self.printer_id, self.name, {
                            "bed_temp": bed_t, "bed_target": bed_tt,
                            "nozzle_temp": noz_t, "nozzle_target": noz_tt,
                            "state": gstate,
                            "progress": progress,
                            "remaining_min": remaining_min,
                            "current_layer": current_layer,
                            "total_layers": total_layers,
                            "fan_speed": fan_speed_val,
                        })
                    except Exception as e:
                        log.debug(f"Failed to push printer state event: {e}")

                # ---- Timeseries telemetry (60s inserts during prints) ----
                if gstate in ('RUNNING', 'PRINTING', 'PAUSE', 'PAUSED') and now - self._last_telemetry_insert >= TELEMETRY_INSERT_INTERVAL:
                    self._last_telemetry_insert = now
                    try:
                        conn.execute(
                            text("INSERT INTO printer_telemetry (printer_id, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed) VALUES (:pid, :bed_t, :noz_t, :bed_tt, :noz_tt, :fan)"),
                            {"pid": self.printer_id, "bed_t": bed_t, "noz_t": noz_t, "bed_tt": bed_tt, "noz_tt": noz_tt, "fan": fan_speed_val})
                        conn.execute(text(f"DELETE FROM printer_telemetry WHERE recorded_at < {sql.now_offset('-90 days')}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- safe: text() uses :param bindings; only sql.* helpers (constants) interpolated via f-string
                    except Exception as e:
                        log.debug(f"[{self.name}] Telemetry insert: {e}")

                # ---- Environment telemetry (5-min inserts for ACE/chamber sensors) ----
                if status.environment_sensors and now - self._last_env_telemetry >= ENV_TELEMETRY_INTERVAL:
                    self._last_env_telemetry = now
                    try:
                        for idx, (sensor_name, temp_val) in enumerate(status.environment_sensors.items()):
                            conn.execute(
                                text("INSERT INTO ams_telemetry (printer_id, ams_unit, humidity, temperature) VALUES (:pid, :unit, :hum, :temp)"),
                                {"pid": self.printer_id, "unit": idx, "hum": None, "temp": temp_val})
                        conn.execute(text(f"DELETE FROM ams_telemetry WHERE recorded_at < {sql.now_offset('-90 days')}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- safe: text() uses :param bindings; only sql.* helpers (constants) interpolated via f-string
                    except Exception as e:
                        log.debug(f"[{self.name}] Env telemetry insert: {e}")

                self._last_heartbeat = now
            except Exception as e:
                log.warning(f"Failed to update telemetry for {self.name}: {e}")

        internal_state = status.internal_state

        # ---- Filament slot sync (every 60s) ----
        if status.filament_slots and now - self._last_slot_sync >= SLOT_SYNC_INTERVAL:
            self._last_slot_sync = now
            self._sync_filament_slots(status.filament_slots)

        # ---- Filament runout detection ----
        if status.filament_detected is not None:
            if self._prev_filament_detected is True and status.filament_detected is False:
                if internal_state in ("RUNNING",):
                    log.warning(f"[{self.name}] Filament runout detected!")
                    printer_events.dispatch_alert(
                        alert_type="filament_runout",
                        severity="warning",
                        title=f"Filament Runout: {self.name}",
                        message=f"Filament sensor triggered on {self.name}",
                        printer_id=self.printer_id,
                    )
            self._prev_filament_detected = status.filament_detected

        # Detect state change
        if internal_state != self._prev_state:
            log.info(f"[{self.name}] State: {self._prev_state} -> {internal_state}")

            # Job started
            if internal_state == "RUNNING" and self._prev_state != "PAUSE":
                self._job_started(status)

            # Job paused
            elif internal_state == "PAUSE" and self._prev_state == "RUNNING":
                log.info(f"[{self.name}] Print paused")

            # Job resumed
            elif internal_state == "RUNNING" and self._prev_state == "PAUSE":
                log.info(f"[{self.name}] Print resumed")

            # Job ended (was running/paused, now idle/error)
            elif self._prev_state in ("RUNNING", "PAUSE") and internal_state in ("IDLE", "FAILED"):
                end_status = "completed" if internal_state == "IDLE" else "failed"
                self._job_ended(end_status, status)
                threading.Thread(target=self._try_dispatch, daemon=True).start()

                # Capture Klipper error message on failure
                if end_status == "failed" and status.error_message:
                    printer_events.record_error(
                        printer_id=self.printer_id,
                        error_code="KLIPPER_ERROR",
                        error_message=status.error_message,
                        source="klipper",
                        severity="error",
                        create_alert=False,  # _job_ended already dispatches alert
                    )

            self._prev_state = internal_state

        # Progress updates while printing
        if internal_state == "RUNNING" and self._current_job_db_id:
            self._update_progress(status)
    
    # ==================== Job Tracking ====================
    
    def _job_started(self, status):
        """Record a new print job starting."""
        # Guard: already tracking a job
        if self._current_job_db_id:
            log.debug(f"[{self.name}] _job_started called but already tracking job {self._current_job_db_id}")
            return

        filename = status.filename or "Unknown"
        total_layers = status.total_layers
        bed_target = status.bed_target
        nozzle_target = status.nozzle_target

        try:
            with engine.begin() as conn:
                # Check for existing running job — resume it instead of creating duplicate
                existing = conn.execute(
                    text("SELECT id, job_name FROM print_jobs "
                    "WHERE printer_id = :pid AND status = 'running' ORDER BY id DESC LIMIT 1"),
                    {"pid": self.printer_id}).fetchone()
                if existing:
                    self._current_job_db_id = existing[0]
                    self._last_filename = existing[1] or filename
                    log.info(f"[{self.name}] Resumed tracking existing job {existing[0]} ({existing[1]})")
                    return

                insert_sql = (
                    "INSERT INTO print_jobs"
                    " (printer_id, job_id, filename, job_name, started_at, status,"
                    "  total_layers, bed_temp_target, nozzle_temp_target)"
                    " VALUES (:pid, :jid, :fname, :jname, :started, 'running', :layers, :bed, :noz)")
                params = {
                    "pid": self.printer_id,
                    "jid": f"mk_{int(time.time())}",
                    "fname": filename,
                    "jname": filename,
                    "started": datetime.now(timezone.utc).isoformat(),
                    "layers": total_layers,
                    "bed": bed_target,
                    "noz": nozzle_target,
                }
                if sql.is_sqlite:
                    conn.execute(text(insert_sql), params)
                    self._current_job_db_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                else:
                    self._current_job_db_id = conn.execute(text(insert_sql + " RETURNING id"), params).scalar()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- safe: text() uses :param bindings; only sql.* helpers (constants) interpolated via f-string
                self._last_filename = filename
            log.info(f"[{self.name}] Job started: {filename} (DB id: {self._current_job_db_id})")

            # Attempt auto-link to scheduled job (same logic as Bambu monitor)
            self._try_auto_link(filename, total_layers)

        except Exception as e:
            log.error(f"[{self.name}] Failed to record job start: {e}")
    
    def _job_ended(self, end_status: str, status):
        """Record a print job ending."""
        import modules.notifications.event_dispatcher as printer_events
        if not self._current_job_db_id:
            log.warning(f"[{self.name}] Job ended but no current job tracked")
            return
        
        try:
            with engine.begin() as conn:
                now_utc = datetime.now(timezone.utc).isoformat()

                # Calculate duration
                duration_seconds = None
                pj_row = conn.execute(
                    text("SELECT started_at FROM print_jobs WHERE id = :jid"),
                    {"jid": self._current_job_db_id}).fetchone()
                if pj_row and pj_row[0]:
                    try:
                        started = datetime.fromisoformat(pj_row[0])
                        ended = datetime.fromisoformat(now_utc)
                        duration_seconds = int((ended - started).total_seconds())
                    except Exception as e:
                        log.debug(f"Failed to parse duration: {e}")

                # Calculate filament used (Klipper reports mm extruded)
                # Convert mm to grams: PLA ~1.24 g/cm³, 1.75mm filament ≈ 2.98g/m
                filament_used_g = None
                if status.filament_used_mm and status.filament_used_mm > 0:
                    filament_used_g = round(status.filament_used_mm / 1000.0 * 2.98, 2)

                conn.execute(text("""
                    UPDATE print_jobs
                    SET ended_at = :ended, status = :status, print_duration_seconds = :dur,
                        filament_used_g = :fil
                    WHERE id = :jid
                """), {"ended": now_utc, "status": end_status, "dur": duration_seconds,
                       "fil": filament_used_g, "jid": self._current_job_db_id})

                # If linked to a scheduled job, update it too
                row = conn.execute(text("""
                    SELECT scheduled_job_id FROM print_jobs WHERE id = :jid
                """), {"jid": self._current_job_db_id}).fetchone()
                if row and row[0]:
                    sched_status = "completed" if end_status == "completed" else "failed"
                    duration_hours = round(duration_seconds / 3600, 4) if duration_seconds else None
                    conn.execute(text("""
                        UPDATE jobs SET status = :status, actual_end = :ended,
                               duration_hours = COALESCE(:dur, duration_hours)
                        WHERE id = :jid
                    """), {"status": sched_status, "ended": now_utc, "dur": duration_hours, "jid": row[0]})
                    log.info(f"[{self.name}] Scheduled job #{row[0]} marked {sched_status}")

                    # Auto-deduct filament if completed
                    if end_status == "completed":
                        self._auto_deduct_filament(conn, row[0])
            log.info(f"[{self.name}] Job {end_status}: DB id {self._current_job_db_id}")

            # Increment care counters on successful completion
            if end_status == "completed":
                try:
                    with engine.connect() as conn2:
                        pj_row = conn2.execute(
                            text("SELECT started_at, ended_at FROM print_jobs WHERE id = :jid"),
                            {"jid": self._current_job_db_id}).fetchone()
                        if pj_row and pj_row[0] and pj_row[1]:
                            from datetime import datetime as dt
                            started = dt.fromisoformat(pj_row[0])
                            ended = dt.fromisoformat(pj_row[1])
                            duration_sec = (ended - started).total_seconds()
                            printer_events.increment_care_counters(self.printer_id, duration_sec / 3600.0, 1)
                except Exception as ce:
                    log.warning(f"[{self.name}] Failed to update care counters: {ce}")
            
            # Dispatch alerts
            job_name = self._last_filename or "Unknown"
            if end_status == "completed":
                printer_events.dispatch_alert(
                    alert_type="print_complete",
                    severity="success",
                    title=f"Print Complete: {job_name}",
                    message=f"Finished on {self.name}",
                    printer_id=self.printer_id,
                )
            else:
                printer_events.dispatch_alert(
                    alert_type="print_failed",
                    severity="error",
                    title=f"Print Failed: {job_name}",
                    message=f"Failed on {self.name}",
                    printer_id=self.printer_id,
                )
                printer_events.record_error(
                    printer_id=self.printer_id,
                    error_code="PRINT_FAILED",
                    error_message=f"Print failed: {job_name}",
                    source="moonraker",
                    severity="error",
                    create_alert=False,
                )

            # Archive the print (completed, failed, or cancelled)
            try:
                from modules.archives.archive import create_print_archive
                create_print_archive(
                    print_job_id=self._current_job_db_id,
                    printer_id=self.printer_id,
                    success=(end_status == 'completed'),
                    result_status=end_status if end_status == 'cancelled' else None,
                )
            except Exception as ae:
                log.warning(f"[{self.name}] Failed to create print archive: {ae}")

            self._current_job_db_id = None
            self._last_filename = ""
            
        except Exception as e:
            log.error(f"[{self.name}] Failed to record job end: {e}")
    
    def _update_progress(self, status):
        """Update progress in DB (throttled)."""
        now = time.time()
        if now - self._last_progress_write < PROGRESS_DB_INTERVAL:
            return
        
        self._last_progress_write = now
        
        try:
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE print_jobs
                    SET progress_percent = :prog, current_layer = :layer, remaining_minutes = :rem
                    WHERE id = :jid
                """), {
                    "prog": status.progress_percent,
                    "layer": status.current_layer,
                    "rem": round(status.print_duration * (100.0 - status.progress_percent) / max(status.progress_percent, 0.1) / 60.0) if status.print_duration and status.progress_percent and status.progress_percent > 1 else None,
                    "jid": self._current_job_db_id,
                })
        except Exception as e:
            log.warning(f"[{self.name}] Progress update failed: {e}")
    
    def _try_dispatch(self):
        """Attempt auto-dispatch of next scheduled job after print completes."""
        try:
            time.sleep(5)
            import printer_dispatch
            printer_dispatch.attempt_dispatch(self.printer_id)
        except Exception as e:
            log.warning(f"[{self.name}] Dispatch attempt error: {e}")

    # ==================== Filament Slot Sync ====================

    def _sync_filament_slots(self, slots):
        """Sync MMU/ACE gate data to filament_slots DB table."""
        try:
            with engine.begin() as conn:
                for slot in slots:
                    slot_num = slot.gate + 1  # DB uses 1-based slot numbers
                    material_key = (slot.material or "").upper()
                    filament_type = _MATERIAL_MAP.get(material_key, material_key or "EMPTY")
                    color_hex = slot.color_hex[:6] if slot.color_hex else None
                    color_name = slot.name or slot.material or None

                    # Check if slot exists
                    existing = conn.execute(
                        text("SELECT id FROM filament_slots WHERE printer_id=:pid AND slot_number=:sn"),
                        {"pid": self.printer_id, "sn": slot_num}).fetchone()

                    if existing:
                        conn.execute(
                            text(f"UPDATE filament_slots SET filament_type=:ft, color=:col, color_hex=:hex, loaded_at={sql.now()} WHERE id=:sid"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- safe: text() uses :param bindings; only sql.* helpers (constants) interpolated via f-string
                            {"ft": filament_type, "col": color_name, "hex": color_hex, "sid": existing[0]})
                    else:
                        conn.execute(
                            text(f"INSERT INTO filament_slots (printer_id, slot_number, filament_type, color, color_hex, loaded_at) VALUES (:pid, :sn, :ft, :col, :hex, {sql.now()})"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- safe: text() uses :param bindings; only sql.* helpers (constants) interpolated via f-string
                            {"pid": self.printer_id, "sn": slot_num, "ft": filament_type, "col": color_name, "hex": color_hex})

                # Remove stale slots beyond current gate count
                max_slot = len(slots)
                if max_slot > 0:
                    conn.execute(
                        text("DELETE FROM filament_slots WHERE printer_id=:pid AND slot_number>:ms"),
                        {"pid": self.printer_id, "ms": max_slot})
        except Exception as e:
            log.debug(f"[{self.name}] Filament slot sync: {e}")

    # ==================== Job Auto-Linking ====================
    
    def _try_auto_link(self, filename: str, total_layers: int):
        """
        Try to link this MQTT-detected print to a scheduled job.
        Same two-strategy approach as Bambu monitor:
          1. Name match
          2. Layer count fingerprint
        """
        try:
            with engine.begin() as conn:
                # Get candidates: scheduled/pending jobs for this printer
                candidates = conn.execute(text("""
                    SELECT j.id as job_id, j.status, pf.filename, pf.original_filename,
                           pf.layer_count, m.name as model_name
                    FROM jobs j
                    JOIN print_files pf ON j.print_file_id = pf.id
                    JOIN models m ON pf.model_id = m.id
                    WHERE j.printer_id = :pid
                      AND j.status IN ('scheduled', 'pending')
                """), {"pid": self.printer_id}).mappings().fetchall()

                if not candidates:
                    return

                # Strategy 1: Name match
                fname_lower = filename.lower() if filename else ""
                for c in candidates:
                    match_names = [
                        (c["filename"] or "").lower(),
                        (c["original_filename"] or "").lower(),
                        (c["model_name"] or "").lower(),
                    ]
                    if fname_lower and any(fname_lower in n or n in fname_lower for n in match_names if n):
                        self._link_job(conn, c["job_id"])
                        log.info(f"[{self.name}] Auto-linked to job #{c['job_id']} (name match)")
                        return

                # Strategy 2: Layer count fingerprint
                if total_layers and total_layers > 0:
                    # Deduplicate by job_id
                    layer_matches = {}
                    for c in candidates:
                        if c["layer_count"] == total_layers and c["job_id"] not in layer_matches:
                            layer_matches[c["job_id"]] = c

                    if len(layer_matches) == 1:
                        job_id = list(layer_matches.keys())[0]
                        self._link_job(conn, job_id)
                        log.info(f"[{self.name}] Auto-linked to job #{job_id} (layer count: {total_layers})")
                        return
                    elif len(layer_matches) > 1:
                        log.warning(
                            f"[{self.name}] Ambiguous layer match ({total_layers} layers) "
                            f"— {len(layer_matches)} candidates, skipping auto-link"
                        )

        except Exception as e:
            log.warning(f"[{self.name}] Auto-link failed: {e}")
    
    def _link_job(self, conn, scheduled_job_id: int):
        """Link the current print_job to a scheduled job."""
        if self._current_job_db_id:
            conn.execute(text("""
                UPDATE print_jobs SET scheduled_job_id = :sjid WHERE id = :jid
            """), {"sjid": scheduled_job_id, "jid": self._current_job_db_id})
            conn.execute(text("""
                UPDATE jobs SET status = 'printing' WHERE id = :jid
            """), {"jid": scheduled_job_id})
    
    def _auto_deduct_filament(self, conn, scheduled_job_id: int):
        """Auto-deduct filament weight when a linked job completes."""
        try:
            row = conn.execute(text("""
                SELECT j.spool_id, pf.filament_weight_grams
                FROM jobs j
                JOIN print_files pf ON j.print_file_id = pf.id
                WHERE j.id = :jid
            """), {"jid": scheduled_job_id}).fetchone()

            if row and row[0] and row[1]:
                spool_id = row[0]
                grams_used = row[1]

                conn.execute(text("""
                    UPDATE spools
                    SET remaining_weight = MAX(0, remaining_weight - :grams)
                    WHERE id = :sid
                """), {"grams": grams_used, "sid": spool_id})

                # Log to spool_usage
                conn.execute(text("""
                    INSERT INTO spool_usage (spool_id, job_id, printer_id, grams_used, created_at)
                    VALUES (:sid, :jid, :pid, :grams, :ts)
                """), {"sid": spool_id, "jid": scheduled_job_id, "pid": self.printer_id,
                       "grams": grams_used, "ts": datetime.now(timezone.utc).isoformat()})

                log.info(f"[{self.name}] Deducted {grams_used}g from spool #{spool_id}")

        except Exception as e:
            log.warning(f"[{self.name}] Filament deduction failed: {e}")


# ------------------------------------------------------------------
# Main — standalone daemon mode (supervisor entrypoint)
# ------------------------------------------------------------------
def start_moonraker_monitors():
    """Load Moonraker printers from DB and start monitors."""
    monitors = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, name, api_host, api_key FROM printers "
                "WHERE api_type='moonraker' AND api_host IS NOT NULL AND is_active=1")
            ).mappings().fetchall()

        for row in rows:
            printer_id = row["id"]
            name = row["name"]
            api_host = (row["api_host"] or "").strip()
            api_key_raw = row["api_key"] or ""

            host, port = api_host, 80
            if ":" in api_host:
                h, prt = api_host.rsplit(":", 1)
                host = h.strip() or host
                try:
                    port = int(prt)
                except Exception as e:
                    log.debug(f"Failed to parse port '{prt}': {e}")
                    port = 80

            api_key = ""
            if api_key_raw:
                try:
                    from core.crypto import decrypt
                    api_key = decrypt(api_key_raw)
                except Exception as e:
                    log.debug(f"Failed to decrypt API key (using raw): {e}")
                    api_key = api_key_raw

            m = MoonrakerMonitor(printer_id=printer_id, name=name, host=host, port=port, api_key=api_key)
            if m.connect():
                monitors.append(m)
                log.info(f"Started Moonraker monitor for {name} ({host}:{port})")
            else:
                log.warning(f"Failed to connect Moonraker monitor for {name} ({host}:{port})")
    except Exception as e:
        log.error(f"Failed to start Moonraker monitors: {e}")
    return monitors

# Supervisor-friendly stop() alias
MoonrakerMonitor.stop = MoonrakerMonitor.disconnect


if __name__ == "__main__":
    import signal

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    _running = True

    def _shutdown(signum, frame):
        global _running
        log.info(f"Received signal {signum}, shutting down Moonraker monitors...")
        _running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Daemon mode: keep running even if no printers exist yet.
    threads = []
    while _running:
        threads = start_moonraker_monitors()
        if not threads:
            log.info("No Moonraker printers found in database — sleeping 60s")
            try:
                time.sleep(60)
            except (KeyboardInterrupt, SystemExit):
                break
            continue

        log.info(f"Monitoring {len(threads)} Moonraker printer(s)")
        try:
            while _running:
                time.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            break

    for t in threads:
        t.stop()
    log.info("Moonraker monitor daemon stopped.")
