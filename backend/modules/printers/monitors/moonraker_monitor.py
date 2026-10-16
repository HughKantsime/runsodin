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
from core.order_production import (prepare_job_write, reconcile_job_write, prepare_print_job_write,
                                   ProductionLinkageChanged, retry_production_linkage)
# Resolved at use time: never a stale engine bound at import.
from core.db import live_engine as engine
from core.db_compat import as_utc_datetime, execute_insert_returning_id, sql

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
# A failed poll is a transient until this many fail in a row (same debounce as
# PrusaLink); then the printer is persisted OFFLINE and reconnection starts.
MAX_CONSECUTIVE_FAILURES = 5
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
        self._marked_offline = False
        self._consecutive_failures = 0
        self._status_lock = threading.RLock()
        self._retired = False
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
        with self._status_lock:
            self._retired = False
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
                log.info("Camera discovery completed for printer %s", self.printer_id)
        except Exception as e:
            log.warning("Camera discovery failed for printer %s", self.printer_id)
    
    def disconnect(self):
        """Stop polling and disconnect."""
        with self._status_lock:
            self._retired = True
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
                if status.state == MoonrakerState.DISCONNECTED:
                    # The adapter swallows transport errors and returns a default
                    # DISCONNECTED status; treat it as a lost connection so the
                    # printer goes OFFLINE without refreshing its heartbeat.
                    raise ConnectionError("Moonraker status poll failed")
                self._process_status(status)
                self._consecutive_failures = 0
            except Exception as e:
                # A failed poll never refreshes the heartbeat; OFFLINE is
                # persisted only after MAX_CONSECUTIVE_FAILURES in a row.
                self._consecutive_failures = getattr(self, '_consecutive_failures', 0) + 1
                log.error(f"[{self.name}] Poll error ({self._consecutive_failures} consecutive): {e}")
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self._consecutive_failures = 0
                    self._handle_disconnect()
            
            time.sleep(POLL_INTERVAL)
    
    def _handle_disconnect(self):
        """Handle lost connection — attempt reconnection."""
        self.printer._connected = False
        self._persist_offline()
        log.warning(f"[{self.name}] Connection lost, retrying in {RECONNECT_INTERVAL}s")
        
        while self._running:
            time.sleep(RECONNECT_INTERVAL)
            if not self._marked_offline:
                self._persist_offline()
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
    
    def _persist_offline(self):
        with self._status_lock:
            if self._retired:
                return
            try:
                with engine.begin() as conn:
                    conn.execute(text("UPDATE printers SET gcode_state='OFFLINE',print_stage='Offline' WHERE id=:pid"), {"pid": self.printer_id})
                self._marked_offline = True
            except Exception:
                log.warning("Offline state persistence failed; retry on next observation")

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
        with self._status_lock:
            if self._retired:
                return
            self._process_status_locked(status)

    def _process_status_locked(self, status):
        """Process a status update — detect state changes, track jobs."""
        import modules.notifications.event_dispatcher as printer_events
        now = time.time()

        # Update telemetry + heartbeat (throttled to every 10 seconds)
        if self._marked_offline or now - getattr(self, '_last_heartbeat', 0) >= 10:
            try:
              with engine.begin() as conn:
                bed_t = bed_tt = noz_t = noz_tt = None
                gstate = None
                stage = 'Idle'
                progress = None
                remaining_min = None
                current_layer = None
                total_layers = None
                # A measured 0% fan is a real reading; only a missing fan object is unknown.
                raw_status = getattr(status, 'raw_data', None) or {}
                fan_speed_val = status.fan_speed if isinstance(raw_status.get('fan'), dict) else None
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
                    text(f"UPDATE printers SET last_seen={sql.now()},"  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                    " bed_temp=COALESCE(:bed_t,bed_temp),bed_target_temp=COALESCE(:bed_tt,bed_target_temp),"
                    " nozzle_temp=COALESCE(:noz_t,nozzle_temp),nozzle_target_temp=COALESCE(:noz_tt,nozzle_target_temp),"
                    " gcode_state=COALESCE(:gstate,gcode_state),print_stage=COALESCE(:stage,print_stage),"
                    " fan_speed=COALESCE(:fan_speed,fan_speed),nozzle_diameter=COALESCE(:noz_dia,nozzle_diameter)"
                    " WHERE id=:pid"),
                    {"bed_t": bed_t, "bed_tt": bed_tt, "noz_t": noz_t, "noz_tt": noz_tt,
                     "gstate": gstate, "stage": stage, "fan_speed": fan_speed_val,
                     "noz_dia": noz_dia, "pid": self.printer_id})

                # ---- Timeseries telemetry (60s inserts during prints) ----
                if gstate in ('RUNNING', 'PRINTING', 'PAUSE', 'PAUSED') and now - self._last_telemetry_insert >= TELEMETRY_INSERT_INTERVAL:
                    self._last_telemetry_insert = now
                    try:
                        conn.execute(
                            text("INSERT INTO printer_telemetry (printer_id, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed) VALUES (:pid, :bed_t, :noz_t, :bed_tt, :noz_tt, :fan)"),
                            {"pid": self.printer_id, "bed_t": bed_t, "noz_t": noz_t, "bed_tt": bed_tt, "noz_tt": noz_tt, "fan": fan_speed_val})
                        conn.execute(text(f"DELETE FROM printer_telemetry WHERE recorded_at < {sql.now_offset('-90 days')}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
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
                        conn.execute(text(f"DELETE FROM ams_telemetry WHERE recorded_at < {sql.now_offset('-90 days')}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                    except Exception as e:
                        log.debug(f"[{self.name}] Env telemetry insert: {e}")

              self._last_heartbeat = now
              self._marked_offline = False

              payload = {
                  'printer_id': self.printer_id, 'bed_temp': bed_t, 'bed_target': bed_tt,
                  'nozzle_temp': noz_t, 'nozzle_target': noz_tt, 'state': gstate,
                  'progress': progress, 'remaining_min': remaining_min,
                  'current_layer': current_layer, 'total_layers': total_layers,
                  'fan_speed': fan_speed_val,
              }
              try:
                  ws_push('printer_telemetry', payload)
              except Exception:
                  log.warning("Committed telemetry WebSocket delivery failed")
              if mqtt_republish:
                  try:
                      mqtt_republish.republish_telemetry(self.printer_id, self.name,
                          {key: value for key, value in payload.items() if key != 'printer_id'})
                  except Exception:
                      log.warning("Committed telemetry MQTT delivery failed")

            except Exception:
                log.warning("Telemetry persistence failed; retry on next observation")

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
                raw_terminal = getattr(status, "raw_print_state", "unknown")
                education_terminal_status = {
                    "complete": "completed",
                    "cancelled": "cancelled",
                    "error": "failed",
                }.get(raw_terminal)
                end_status = education_terminal_status or (
                    "completed" if internal_state == "IDLE" else "failed"
                )
                education_suppressed = self._job_ended(
                    end_status,
                    status,
                    education_terminal_status=education_terminal_status,
                )
                if not education_suppressed:
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
                        history=True,
                    )

            self._prev_state = internal_state

        # Progress updates while printing
        if internal_state == "RUNNING" and self._current_job_db_id:
            self._update_progress(status)
    
    # ==================== Job Tracking ====================
    
    @retry_production_linkage
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
                from core.registry import registry

                from core.attempt_start_observation import observe_start
                observed=observe_start(conn,printer_id=self.printer_id,protocol='moonraker',
                    filename=filename,job_name=filename,physical_values={
                        'job_id':f'mk_{int(time.time())}',
                        'started_at':datetime.now(timezone.utc).isoformat(),
                        'total_layers':total_layers,'bed_temp_target':bed_target,
                        'nozzle_temp_target':nozzle_target},source='moonraker-first-start',allow_physical_resume=True)
                self._current_job_db_id=observed.id
                self._last_filename=filename
                education_sensitive=observed.education_sensitive
                generic_reserved=observed.reserved
            if not observed.fresh:
                return
            log.info(f"[{self.name}] Job started: {filename} (DB id: {self._current_job_db_id})")

            # Attempt auto-link to scheduled job (same logic as Bambu monitor)
            if not education_sensitive and not generic_reserved:
                self._try_auto_link(filename, total_layers)
            if not education_sensitive:
                from core.dispatch_observations import publish_observed_start
                publish_observed_start(self.printer_id,self._current_job_db_id,filename)

        except ProductionLinkageChanged:
            self._current_job_db_id = None
            raise
        except Exception as e:
            self._current_job_db_id = None
            log.error(f"[{self.name}] Failed to record job start: {e}")
    
    @retry_production_linkage
    def _job_ended(
        self, end_status: str, status, *, education_terminal_status: str | None = None
    ) -> bool:
        """Record a print job ending."""
        import modules.notifications.event_dispatcher as printer_events
        if not self._current_job_db_id:
            log.warning(f"[{self.name}] Job ended but no current job tracked")
            return False
        
        ended_observation_id=self._current_job_db_id
        committed_completion = False
        completion_job_id = None
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
                        started = as_utc_datetime(pj_row[0])
                        ended = as_utc_datetime(now_utc)
                        duration_seconds = int((ended - started).total_seconds())
                    except Exception as e:
                        log.debug(f"Failed to parse duration: {e}")

                from core.registry import registry

                policy = registry.get_provider("EducationPolicyProvider")
                classification = (
                    policy.classify_monitor_observation(
                        conn,
                        printer_id=self.printer_id,
                        print_job_id=getattr(self, '_current_job_db_id', None),
                        observed_filename=status.filename or self._last_filename,
                    )
                    if policy
                    else None
                )
                if classification and (
                    classification.get("education_owned")
                    or classification.get("education_reserved")
                ):
                    if education_terminal_status is None:
                        log.error(
                            "[%s] Ambiguous Moonraker terminal state %s; Education authority unchanged",
                            self.name,
                            getattr(status, "raw_print_state", "unknown"),
                        )
                        return True
                    result = policy.terminal_monitor_observation(
                        conn,
                        print_job_id=getattr(self, '_current_job_db_id', None),
                        printer_id=self.printer_id,
                        terminal_status=education_terminal_status,
                        ended_at=now_utc,
                        duration_seconds=duration_seconds,
                        error_code=status.error_message or None,
                    )
                    if result.get("education_owned") or result.get("education_reserved"):
                        conn.commit()
                        # Physical completion whenever the print really ended (also when
                        # Education refused the job transition); the job only if applied.
                        applied = result.get('authorized') is True and result.get('transitioned') is True
                        physical = applied or result.get('physical_ended') is True
                        committed_completion = education_terminal_status == 'completed' and physical
                        completion_job_id = result.get('job_id') if applied else None
                        log.info("[%s] Education terminal result: %s", self.name, result)
                        if physical:
                            try:
                                from modules.archives.archive import create_print_archive
                                create_print_archive(print_job_id=self._current_job_db_id, printer_id=self.printer_id,
                                                     success=(education_terminal_status == 'completed'),
                                                     result_status=(education_terminal_status
                                                                    if education_terminal_status == 'cancelled' else None))
                            except Exception as ae:
                                log.warning(f"[{self.name}] Failed to create print archive: {ae}")
                        self._current_job_db_id = None
                        self._last_filename = ""
                        return True

                _, production_orders = prepare_print_job_write(conn, self._current_job_db_id)
                prior = conn.execute(text('SELECT status FROM print_jobs WHERE id=:id'),
                                     {'id':self._current_job_db_id}).first()
                if prior is None or prior[0] in ('completed','failed','cancelled'):
                    self._current_job_db_id = None
                    self._last_filename = ''
                    return False  # Duplicate observations produce no effects.

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
                ended = False
                if row and row[0]:
                    sched_status = end_status if end_status in ('completed','failed','cancelled') else 'failed'
                    duration_hours = round(duration_seconds / 3600, 4) if duration_seconds else None
                    # Never overwrite an operator's terminal decision for the job.
                    from modules.jobs.services import transition_job, Expr
                    ended = transition_job(conn, row[0], sched_status, 'monitor', extra_fields={
                        "actual_end": now_utc,
                        "duration_hours": Expr("COALESCE(duration_hours, :dur)", dur=duration_hours)})
                    log.info(f"[{self.name}] Scheduled job #{row[0]} "
                             + (f"marked {sched_status}" if ended else "already terminal; kept operator status"))

                    # Auto-deduct filament if completed
                    if end_status == "completed" and ended:
                        self._auto_deduct_filament(conn, row[0], observed_grams=filament_used_g)
                reconcile_job_write(conn, production_orders)
            # Physical completion always; the job only when its transition applied.
            committed_completion = end_status == 'completed'
            completion_job_id = row[0] if row and row[0] and ended else None
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
                            started = as_utc_datetime(pj_row[0])
                            ended = as_utc_datetime(pj_row[1])
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
                    print_job_id=getattr(self, '_current_job_db_id', None),
                    printer_id=self.printer_id,
                    success=(end_status == 'completed'),
                    result_status=end_status if end_status == 'cancelled' else None,
                )
            except Exception as ae:
                log.warning(f"[{self.name}] Failed to create print archive: {ae}")

            self._current_job_db_id = None
            self._last_filename = ""
            return False
            
        except ProductionLinkageChanged:
            raise
        except Exception as failure:
            log.error("[%s] Failed to record job end (%s); reconciliation required",
                      self.name, type(failure).__name__)
            return False
        finally:
            from core.dispatch_claims import retire_observed_terminal
            try:
                retire_observed_terminal(engine,printer_id=self.printer_id,print_job_id=ended_observation_id)
            finally:
                if committed_completion:
                    from modules.printers.committed_terminal_events import publish_committed_completion
                    publish_committed_completion(self.printer_id, ended_observation_id, completion_job_id)
    
    def _update_progress(self, status):
        """Update progress in DB (throttled)."""
        now = time.time()
        if now - self._last_progress_write < PROGRESS_DB_INTERVAL:
            return
        
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
            self._last_progress_write = now
        except Exception:
            log.warning("Printer progress persistence failed; will retry")
    
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
                            text(f"UPDATE filament_slots SET filament_type=:ft, color=:col, color_hex=:hex, loaded_at={sql.now()} WHERE id=:sid"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                            {"ft": filament_type, "col": color_name, "hex": color_hex, "sid": existing[0]})
                    else:
                        conn.execute(
                            text(f"INSERT INTO filament_slots (printer_id, slot_number, filament_type, color, color_hex, loaded_at) VALUES (:pid, :sn, :ft, :col, :hex, {sql.now()})"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
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
                      AND NOT EXISTS (
                          SELECT 1 FROM education_submissions s WHERE s.job_id=j.id
                      )
                """), {"pid": self.printer_id}).mappings().fetchall()

                if not candidates:
                    return

                # Lock all candidate parents once in sorted order before trying
                # alternatives, avoiding candidate-order parent deadlocks.
                prepare_job_write(conn, [candidate['job_id'] for candidate in candidates])

                # Strategy 1: Name match
                fname_lower = filename.lower() if filename else ""
                for c in candidates:
                    match_names = [
                        (c["filename"] or "").lower(),
                        (c["original_filename"] or "").lower(),
                        (c["model_name"] or "").lower(),
                    ]
                    if fname_lower and any(fname_lower in n or n in fname_lower for n in match_names if n):
                        if self._link_job(conn, c["job_id"]):
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
                        if self._link_job(conn, job_id):
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
        """Link only current generic queued work, inside the caller transaction."""
        if not self._current_job_db_id:
            return False
        from core.order_production import (prepare_job_write, reconcile_job_write, prepare_print_job_write,
                                   ProductionLinkageChanged, retry_production_linkage)
        parents = prepare_job_write(conn, [scheduled_job_id])
        # Parent -> print record -> job is the same order as terminal handlers.
        print_query = text("SELECT printer_id,scheduled_job_id,status FROM print_jobs WHERE id=:id")
        if conn.dialect.name == 'postgresql':
            print_query = text(str(print_query) + " FOR UPDATE")
        current = conn.execute(print_query, {'id':self._current_job_db_id}).first()
        if (not current or current[0] != self.printer_id or current[2] not in ('running', 'printing')
                or current[1] not in (None, scheduled_job_id)):
            return False
        from modules.jobs.services import transition_job
        changed = transition_job(conn, scheduled_job_id, 'printing', 'monitor',
            from_states=('scheduled', 'pending'),
            where="printer_id=:pid"
                  " AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=jobs.id)"
                  " AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.print_file_id=jobs.print_file_id)",
            params={'pid': self.printer_id}) or None
        if changed is None:
            return False
        linked = conn.execute(text("""
            UPDATE print_jobs SET scheduled_job_id=:job WHERE id=:id AND printer_id=:pid
              AND status IN ('running','printing') AND (scheduled_job_id IS NULL OR scheduled_job_id=:job)
            RETURNING id
        """), {'job':scheduled_job_id,'id':self._current_job_db_id,'pid':self.printer_id}).first()
        if linked is None:
            raise RuntimeError('Print linkage changed; transaction must roll back')
        from core.print_attempts import bind_print_attempt, sqlalchemy_runner
        bind_print_attempt(sqlalchemy_runner(conn), self._current_job_db_id, scheduled_job_id)
        reconcile_job_write(conn, parents)
        return True

    def _auto_deduct_filament(self, conn, scheduled_job_id: int, *, observed_grams=None):
        """Account for one unambiguous loaded spool using actual schema fields.

        Klipper's measured extrusion is preferred. A stored sliced weight is an
        estimate fallback. Multi-spool attribution requires explicit mapping;
        never guess which spool supplied a multi-material print.
        """
        import math
        job = conn.execute(text("""
            SELECT j.printer_id,j.charged_to_org_id,pf.filament_weight_grams,pf.filaments_json
            FROM jobs j LEFT JOIN print_files pf ON j.print_file_id=pf.id WHERE j.id=:id
        """), {'id':scheduled_job_id}).first()
        if not job or job[0] != self.printer_id:
            return
        if job[3]:
            import json
            try:
                materials = json.loads(job[3]) if isinstance(job[3],str) else job[3]
                if not isinstance(materials,list) or len(materials)>1:
                    return
            except (ValueError, TypeError):
                return
        grams = observed_grams if observed_grams is not None else job[2]
        if grams is None or not math.isfinite(float(grams)) or grams <= 0:
            return
        spools = conn.execute(text("""
            SELECT id FROM spools WHERE location_printer_id=:printer AND status='active'
              AND (org_id IS NULL OR org_id=:org) ORDER BY id
        """), {'printer':self.printer_id,'org':job[1]}).fetchall()
        if len(spools) != 1:
            return
        # The filament ledger: once per print attempt, shared with operator
        # completion and archive accounting; claimed only if something is deducted.
        from core.filament_ledger import flag_reconciliation, record_job_filament
        from core.print_attempts import sqlalchemy_runner
        print_job_id = getattr(self, '_current_job_db_id', None)
        outcome = record_job_filament(sqlalchemy_runner(conn), scheduled_job_id, source="moonraker",
                                      grams_by_spool={spools[0][0]: float(grams)}, print_job_id=print_job_id,
                                      notes='observed extrusion' if observed_grams is not None else 'sliced estimate')
        if outcome.unattributed and print_job_id is not None:
            # Never silently drop it: the archive path records this as unresolved;
            # here the ledger keeps a durable reconciliation flag.
            log.error("[%s] Filament for print %s of job %s not deducted (%s); reconciliation required",
                      getattr(self, 'name', self.printer_id), print_job_id, scheduled_job_id, outcome.reason)
            flag_reconciliation(sqlalchemy_runner(conn), scheduled_job_id, print_job_id,
                                reason=outcome.reason, source="moonraker")
        return outcome


# ------------------------------------------------------------------
# Main — standalone daemon mode (supervisor entrypoint)
# ------------------------------------------------------------------
def start_moonraker_monitors():
    """Load Moonraker printers from DB and start monitors."""
    from core.monitor_bootstrap import bootstrap_monitor_authority
    bootstrap_monitor_authority()
    monitors = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, name, api_host, api_key FROM printers "
                "WHERE api_type='moonraker' AND api_host IS NOT NULL AND is_active IS TRUE")
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
    from core.log_redaction import install_log_redaction
    install_log_redaction()
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
