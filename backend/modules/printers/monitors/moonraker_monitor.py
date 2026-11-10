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

from core.base import AlertType, AlertSeverity
from core.base import slot_filament_value

import os
import sys
import time
import logging
import threading
from datetime import datetime, timedelta, timezone
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



from modules.printers.monitors.moonraker_job_tracking import MoonrakerJobTracking


class MoonrakerMonitor(MoonrakerJobTracking):
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
        self._unwatched_row = None
        self._first_look = None
        self._first_look_open = True
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
            self._recover_after_reconnect()
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
        """Let the first live observation validate identity and elapsed time.

        Never recover a row from database status alone. The start observer can
        resume a plausible row, or close stale rows without terminal effects.
        """
        with self._status_lock:
            self._current_job_db_id = None
            self._last_filename = ""
            self._prev_state = None
            self._first_look = None
            self._first_look_open = True

    # ==================== Status Processing ====================
    
    def _process_status(self, status):
        with self._status_lock:
            if self._retired:
                return
            tracked = self._current_job_db_id
            filename = self._last_filename
            terminal = self._prev_state in ('RUNNING', 'PAUSE') and status.internal_state in ('IDLE', 'FAILED')
            raw_state = getattr(status, 'raw_print_state', None)
            first_poll = self._prev_state is None
            # First look after a restart or reconnect, and the print is already over. The first look
            # lasts through states that don't say whether it was printing: some firmware reports
            # standby or stopping for a moment before complete or cancelled.
            ended_unwatched = (raw_state in ('complete', 'cancelled', 'error') and getattr(self, '_first_look_open', first_poll)
                               and not tracked and bool(status.filename))
            # Its end failed to commit: a retry still places history by the row, not by now.
            retrying_unwatched = terminal and tracked and tracked == getattr(self, '_unwatched_row', None)
        if first_poll:
            self._note_first_look()
        # Network I/O (adapter timeout 5s) holds neither status lock nor DB transaction.
        from modules.printers.monitors.moonraker_history import terminal_history
        history = terminal_history(self.printer, filename, time.time()) if terminal and tracked and not retrying_unwatched else None
        if ended_unwatched or retrying_unwatched:
            history = self._unwatched_end_history(status)
        with self._status_lock:
            if self._retired or self._current_job_db_id != tracked:
                return
            retry = ended_unwatched and not self._adopt_unwatched_end(status, history)
            self._process_status_locked(status, history=history)
            if retry:
                self._prev_state = None
            elif raw_state in ('printing', 'paused', 'complete', 'cancelled', 'error'):
                self._first_look_open = False

    def _note_first_look(self):
        """This poll's heartbeat replaces last_seen, and a later poll can still be the first look."""
        try:
            with engine.connect() as conn:
                self._first_look_last_seen(conn)
        except Exception:
            log.debug(f"[{self.name}] First look not recorded; the age rule will refuse rather than guess")

    def _first_look_last_seen(self, conn):
        """printers.last_seen as the first look found it; this monitor's heartbeat replaces it."""
        if getattr(self, '_first_look', None) is None:
            self._first_look = (conn.execute(text('SELECT last_seen FROM printers WHERE id=:pid'), {'pid': self.printer_id}).scalar_one_or_none(),)
        return self._first_look[0]

    def _unwatched_end_history(self, status):
        """History for the one running row a first-seen end could belong to, if any."""
        from modules.printers.monitors.moonraker_history import terminal_history
        try:
            with engine.connect() as conn:
                self._first_look_last_seen(conn)
                rows = conn.execute(text("SELECT started_at FROM print_jobs WHERE printer_id=:pid AND status='running' "
                                         "AND (filename=:name OR job_name=:name) LIMIT 2"),
                                    {'pid': self.printer_id, 'name': status.filename}).all()
            if len(rows) != 1:
                return None
            return terminal_history(self.printer, status.filename, time.time(),
                                    row_started_at=as_utc_datetime(rows[0][0]).timestamp())
        except Exception:
            return None  # History is optional; the raw state still names the outcome.

    def _adopt_unwatched_end(self, status, history):
        """Track the one row a first-seen end belongs to, so the live end branch closes it.

        Physical-resume identity rules. Inserts nothing and closes nothing else.
        False only when the lookup itself failed: the next poll tries again.
        """
        from core.dispatch_observations import generic_observation_reserved, prepare_generic_observation
        from core.interfaces.education_policy import is_education_reserved_filename
        from core.physical_print import same_physical_print
        filename = status.filename
        if is_education_reserved_filename(filename):
            return True
        try:
            with engine.begin() as conn:
                if generic_observation_reserved(conn, printer_id=self.printer_id, observed_filename=filename):
                    identifier = prepare_generic_observation(conn, printer_id=self.printer_id, observed_filename=filename)
                else:
                    rows = conn.execute(text("SELECT p.id,p.filename,p.job_name,p.started_at FROM print_jobs p "
                                             "WHERE p.printer_id=:pid AND p.status='running' "
                                             "AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=p.scheduled_job_id)"),
                                        {'pid': self.printer_id}).mappings().all()
                    # A matched history entry already placed the row inside that print. Without
                    # one, the row's age when ODIN last saw this printer must fit inside the print.
                    last_seen = self._first_look_last_seen(conn)
                    elapsed = None if history else getattr(status, 'total_duration', None)
                    plausible = [row for row in rows if same_physical_print(
                        row, filename, elapsed, now=as_utc_datetime(last_seen) if last_seen else None)]
                    identifier = plausible[0]['id'] if len(plausible) == 1 else None
        except Exception as failure:
            log.warning("[%s] First-seen print end lookup failed (%s)", self.name, type(failure).__name__)
            return False
        if identifier is not None:
            self._current_job_db_id = identifier
            self._last_filename = filename
            self._prev_state = 'RUNNING'
            self._unwatched_row = identifier
            log.info(f"[{self.name}] Print {identifier} ended while unwatched")
        return True

    def _process_status_locked(self, status, *, history=None):
        """Process a status update — detect state changes, track jobs."""
        import modules.notifications.event_dispatcher as printer_events
        now = time.time()

        # Update telemetry + heartbeat (throttled to every 10 seconds)
        live_state_allowed = True
        if self._marked_offline or now - getattr(self, '_last_heartbeat', 0) >= 10:
            live_state_allowed = False
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

              live_state_allowed = True
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

        # Keep optional evidence separate and after a successful status commit;
        # reports between heartbeats still refresh live-state freshness.
        if live_state_allowed:
            from modules.printers.monitors.live_state import observe_status
            observe_status(engine, self.printer_id, status, 'moonraker', owner=self)

        internal_state = status.internal_state

        # ---- Filament slot sync (every 60s) ----
        if status.filament_slots and now - self._last_slot_sync >= SLOT_SYNC_INTERVAL:
            self._last_slot_sync = now
            self._sync_filament_slots(status.filament_slots)

        # ---- Filament runout detection ----
        if status.filament_detected is not None:
            if self._prev_filament_detected is True and status.filament_detected is False:
                if internal_state in ("RUNNING",):
                    from core.job_timeline import for_printer
                    for_printer(self.printer_id, "runout")
                    log.warning(f"[{self.name}] Filament runout detected!")
            self._prev_filament_detected = status.filament_detected

        # Detect state change
        if internal_state != self._prev_state:
            if internal_state != "PAUSE":
                from core.job_timeline import close_pause_episode
                close_pause_episode(self.printer_id)
            log.info(f"[{self.name}] State: {self._prev_state} -> {internal_state}")

            # Job started
            if internal_state == "RUNNING" and self._prev_state != "PAUSE":
                self._job_started(status)

            # Job paused
            elif internal_state == "PAUSE":
                if not self._current_job_db_id:
                    self._job_started(status)
                from core.job_timeline import for_printer
                for_printer(self.printer_id, "paused", context={"filename": status.filename,
                            "pause_reason": getattr(status, "error_message", None) or ""})
                log.info(f"[{self.name}] Print paused")

            # Job resumed
            elif internal_state == "RUNNING" and self._prev_state == "PAUSE":
                from core.job_timeline import for_printer
                for_printer(self.printer_id, "resumed")
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
                    education_terminal_status=education_terminal_status, history=history,
                )
                if not education_suppressed and self._current_job_db_id:
                    # Nothing committed. Moving on would leave a stale tracked row that makes the
                    # next print look already tracked; the previous state retries this end instead.
                    return
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
    


# ------------------------------------------------------------------
# Main — standalone daemon mode (supervisor entrypoint)
# ------------------------------------------------------------------
def _endpoint(api_host):
    api_host = (api_host or "").strip()
    host, port = api_host, 80
    if ":" in api_host:
        h, prt = api_host.rsplit(":", 1)
        host = h.strip() or host
        try:
            port = int(prt)
        except Exception as e:
            log.debug(f"Failed to parse port '{prt}': {e}")
            port = 80
    return host, port


def _api_key(api_key_raw):
    if not api_key_raw:
        return ""
    try:
        from core.crypto import decrypt
        return decrypt(api_key_raw)
    except Exception as e:
        log.debug(f"Failed to decrypt API key (using raw): {e}")
        return api_key_raw


def _rows():
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id, name, api_host, api_key FROM printers "
            "WHERE api_type='moonraker' AND api_host IS NOT NULL AND is_active IS TRUE")
        ).mappings().fetchall()


def _inventory():
    """{printer_id: (fingerprint, (name, host, port, api_key))} for license retirement (B2c)."""
    from core.license_monitor import connection_fingerprint
    inventory = {}
    for row in _rows():
        host, port = _endpoint(row["api_host"])
        api_key = _api_key(row["api_key"] or "")
        inventory[row["id"]] = (connection_fingerprint("moonraker", host, port, api_key),
                                (row["name"], host, port, api_key))
    return inventory


class _MoonrakerSession(threading.Thread):
    """License fleet lock enabled (B2c): one printer's session. Connects on its own
    thread and retries a failed connect every RECONNECT_INTERVAL; once connected,
    the monitor's poll loop handles reconnection. Never stopped in-process for a
    license or configuration change: the daemon exits and restarts instead."""

    def __init__(self, printer_id, name, host, port, api_key):
        super().__init__(name=f"moonraker-session-{name}", daemon=True)
        self.monitor = MoonrakerMonitor(printer_id=printer_id, name=name, host=host, port=port, api_key=api_key)
        self._stopped = threading.Event()

    def run(self):
        while not self._stopped.is_set():
            if self.monitor.connect():
                log.info(f"Started Moonraker monitor for {self.monitor.name}")
                poll = self.monitor._thread
                if poll is not None:
                    poll.join()
                return
            log.warning(f"Failed to connect Moonraker monitor for {self.monitor.name}; retrying in {RECONNECT_INTERVAL}s")
            time.sleep(RECONNECT_INTERVAL)

    def stop(self):
        self._stopped.set()
        self.monitor.disconnect()


def start_moonraker_monitors():
    """Load Moonraker printers from DB and start monitors (license fleet lock off; see __main__)."""
    from core.monitor_bootstrap import bootstrap_monitor_authority
    bootstrap_monitor_authority()
    monitors = []
    try:
        for row in _rows():
            printer_id = row["id"]
            name = row["name"]
            host, port = _endpoint(row["api_host"])
            api_key = _api_key(row["api_key"] or "")

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
    from core.schema.bootstrap import configure_orm
    configure_orm()
    from core.job_timeline import start
    start("moonraker_monitor")
    import signal

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    from core.interfaces.filament_ownership import log_external_filament_accounting
    log_external_filament_accounting(log)

    shutdown = threading.Event()

    def _shutdown(signum, frame):
        log.info(f"Received signal {signum}, shutting down Moonraker monitors...")
        shutdown.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # License fleet lock enabled: restart retirement (B2c, core.license_monitor).
    # Otherwise the monitors start once (retried every 60 s while none exist), as before.
    from core.license_monitor import run_monitor_process
    run_monitor_process(engine, _inventory, _MoonrakerSession, start_moonraker_monitors, 'Moonraker', shutdown, log)
    log.info("Moonraker monitor daemon stopped.")
