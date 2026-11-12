"""
OctoPrint Monitor — polling daemon for OctoPrint servers (Experimental).

Built from OctoPrint's public REST API documentation only (see adapters/octoprint.py).

Architecture:
  - One thread per OctoPrint printer.
  - Polls GET /api/printer and GET /api/job every POLL_INTERVAL seconds.
  - Feeds the same shared handlers the PrusaLink monitor uses: the event_dispatcher
    on_print_* functions (job tracking, start/end observation) and live_state.observe_status.
    Nothing from those paths is copied here.
  - Fail closed: an OFFLINE (HTTP 409) or UNKNOWN reading is recorded as such, never advances
    last_seen and never starts or ends a print. A print is recorded as completed only when
    OctoPrint reports 100 % completion; otherwise it is recorded as cancelled.
"""

import time
import logging
import threading

from sqlalchemy import text

# Resolved at use time: never a stale engine bound at import.
from core.db import live_engine as engine
from core.db_compat import sql

try:
    from core.ws_hub import push_event as ws_push
except ImportError:
    def ws_push(*a, **kw): pass

try:
    import modules.notifications.mqtt_republish as _mqtt_republish
except ImportError:
    _mqtt_republish = None

from modules.printers.adapters.octoprint import OctoPrintPrinter, OctoPrintState

log = logging.getLogger(__name__)

POLL_INTERVAL = 5            # seconds between polls
HEARTBEAT_INTERVAL = 10      # seconds between telemetry + last_seen writes
PROGRESS_DB_INTERVAL = 5     # at most one progress write per this many seconds
MAX_CONSECUTIVE_FAILURES = 5  # Treat as disconnect after this many failures
MAX_UNREAD_JOB_POLLS = 10     # PRINTING -> IDLE waits this many polls for a readable /api/job

_ODIN_STATE = {"PRINTING": "RUNNING", "PAUSED": "PAUSE", "ERROR": "FAILED"}


class OctoPrintMonitorThread(threading.Thread):
    """Monitor thread for a single OctoPrint printer."""

    def __init__(self, printer_id: int, name: str, host: str, port: int = 80, api_key: str = ""):
        super().__init__(daemon=True)
        self.printer_id = printer_id
        self.name = name
        self.host = host
        self.client = OctoPrintPrinter(host=host, port=port, api_key=api_key)
        self._running = True
        self._consecutive_failures = 0
        self._last_heartbeat = 0
        self._last_state = None
        self._last_filename = None
        self._last_progress = 0.0
        self._last_progress_write = 0
        self._last_telemetry_insert = 0
        self._marked_offline = False
        self._offline_written = False  # OFFLINE is in the row now (distinct from the heartbeat flag)
        self._gap_state = None
        self._unread_polls = 0
        self._status_lock = threading.RLock()
        self._retired = False

    def stop(self):
        with self._status_lock:
            self._retired = True
            self._running = False

    def run(self):
        log.info(f"[{self.name}] OctoPrint monitor started for {self.host}")
        self._discover_and_save_camera()
        while self._running:
            try:
                status = self.client.get_status()
                if status.state == OctoPrintState.DISCONNECTED:
                    # No usable answer: count it as a failed poll so OFFLINE is only
                    # persisted after MAX_CONSECUTIVE_FAILURES in a row.
                    raise ConnectionError("OctoPrint status poll failed")
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.info(f"[{self.name}] Connection recovered, re-discovering camera...")
                    self._discover_and_save_camera()
                self._consecutive_failures = 0
                self._process_status(status)
            except Exception:
                self._consecutive_failures += 1
                with self._status_lock:
                    self._gap_state = None  # a later UNKNOWN/OFFLINE reading must be written again
                    if not self._retired:
                        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self._offline_written:
                            if self._persist_state("OFFLINE", "Offline"):
                                self._marked_offline = True
                                self._offline_written = True
                log.warning("Printer poll failed (%s consecutive failures)", self._consecutive_failures)
            time.sleep(POLL_INTERVAL)
        log.info(f"[{self.name}] OctoPrint monitor stopped")

    def _persist_state(self, gstate: str, stage: str) -> bool:
        """Record a non-reading state. Deliberately leaves last_seen alone."""
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE printers SET gcode_state=:gstate, print_stage=:stage WHERE id=:pid"),
                    {"gstate": gstate, "stage": stage, "pid": self.printer_id},
                )
            return True
        except Exception:
            log.warning("State persistence failed; retry on next observation")
            return False

    def _try_dispatch(self):
        """Attempt auto-dispatch of next scheduled job after print completes."""
        try:
            time.sleep(5)
            from modules.printers import dispatch as printer_dispatch
            printer_dispatch.attempt_dispatch(self.printer_id)
        except Exception as e:
            log.warning(f"[{self.name}] Dispatch attempt error: {e}")

    def _discover_and_save_camera(self):
        """Register the stream URL OctoPrint reports in its own settings, if any."""
        import modules.notifications.event_dispatcher as printer_events
        try:
            cam_url = self.client.get_webcam_url()
            if cam_url:
                printer_events.discover_camera(self.printer_id, cam_url)
                log.info("Camera discovery completed for printer %s", self.printer_id)
        except Exception:
            log.warning("Camera discovery failed for printer %s", self.printer_id)

    def _process_status(self, status):
        with self._status_lock:
            if self._retired:
                return
            self._process_status_locked(status)

    def _process_status_locked(self, status):
        """Process polled status: update DB, detect transitions, push events."""
        import modules.notifications.event_dispatcher as printer_events

        # ----------------------------------------------------------
        # 0. Not a reading: record it, change nothing else
        # ----------------------------------------------------------
        if status.state in (OctoPrintState.OFFLINE, OctoPrintState.UNKNOWN):
            gstate = status.internal_state
            if self._gap_state != gstate:
                stage = "Offline" if gstate == "OFFLINE" else "Unknown"
                if self._persist_state(gstate, stage):
                    self._gap_state = gstate
                    self._marked_offline = True  # the next real reading writes its heartbeat at once
                    self._offline_written = gstate == "OFFLINE"
            return
        self._gap_state = None

        # ----------------------------------------------------------
        # 1. Detect state transitions via the shared handlers
        # ----------------------------------------------------------
        current_state = status.internal_state  # IDLE | PRINTING | PAUSED | ERROR
        current_file = status.filename
        first_look = not getattr(self, '_known_state_seen', False)
        self._known_state_seen = True
        odin_state = _ODIN_STATE.get(current_state, "IDLE")
        remaining_min = status.time_remaining // 60 if status.time_remaining else None

        if current_state != "IDLE":
            self._unread_polls = 0
        if current_state != self._last_state:
            previous = self._last_state
            deferred = False  # PRINTING -> IDLE waits for a readable /api/job
            if odin_state != "PAUSE":
                from core.job_timeline import close_pause_episode
                close_pause_episode(self.printer_id)
            if odin_state == "RUNNING" and previous == "PAUSED":
                from core.job_timeline import for_printer
                for_printer(self.printer_id, 'resumed')
            elif odin_state == "RUNNING":
                from core.physical_print import continuing_print_limit
                printer_events.on_print_start(
                    self.printer_id,
                    current_file or "Unknown",
                    0,
                    None,
                    remaining_min,
                    observed_elapsed=continuing_print_limit(status.time_printing, first_look=first_look),
                )
            elif odin_state == "IDLE" and previous in ("PRINTING", "PAUSED"):
                # OctoPrint has no "finished" flag. Completed needs OctoPrint's own 100 % from a
                # job reading that succeeded. A failed reading decides nothing: wait and retry,
                # up to MAX_UNREAD_JOB_POLLS polls, then record cancelled with that reason.
                if not status.job_read:
                    self._unread_polls += 1
                if status.job_read or self._unread_polls >= MAX_UNREAD_JOB_POLLS:
                    finished = (status.job_read and status.progress_percent is not None
                                and status.progress_percent >= 100.0)
                    if finished:
                        printer_events.on_print_complete(
                            self.printer_id,
                            current_file or self._last_filename or "Unknown",
                            status.time_printing,
                            None,
                        )
                    else:
                        printer_events.on_print_cancelled(
                            self.printer_id,
                            current_file or self._last_filename or "Unknown",
                        )
                        if not status.job_read:
                            try:
                                printer_events.record_error(
                                    self.printer_id,
                                    error_code="OCTOPRINT_RESULT_UNREADABLE",
                                    error_message="The print result could not be read from OctoPrint; recorded as cancelled",
                                    source="octoprint",
                                    severity="warning",
                                    history=True,
                                )
                            except Exception as e:
                                log.debug(f"Failed to record OctoPrint unreadable result: {e}")
                    self._unread_polls = 0
                    threading.Thread(target=self._try_dispatch, daemon=True).start()
                else:
                    deferred = True
            elif odin_state == "FAILED":
                error_msg = "Printer reported error state"
                # An error with no print running (idle printer, serial port gone) ends nothing and
                # must not start an auto-dispatch; the error itself is still recorded below.
                if previous in ("PRINTING", "PAUSED"):
                    printer_events.on_print_failed(
                        self.printer_id,
                        current_file or self._last_filename or "Unknown",
                        error_msg,
                    )
                    threading.Thread(target=self._try_dispatch, daemon=True).start()
                try:
                    printer_events.record_error(
                        self.printer_id,
                        error_code=f"OCTOPRINT_{current_state}",
                        error_message=error_msg,
                        source="octoprint",
                        severity="error",
                        history=True,
                    )
                except Exception as e:
                    log.debug(f"Failed to record OctoPrint error: {e}")
            elif odin_state == "PAUSE":
                printer_events.on_print_paused(self.printer_id, current_file)

            if not deferred:
                self._last_state = current_state

        if current_file:
            self._last_filename = current_file

        # ----------------------------------------------------------
        # 2. Progress (while printing): at most one write per PROGRESS_DB_INTERVAL
        # ----------------------------------------------------------
        if status.state == OctoPrintState.PRINTING and status.progress_percent:
            now = time.time()
            if (abs(status.progress_percent - self._last_progress) >= 0.1
                    and now - self._last_progress_write >= PROGRESS_DB_INTERVAL):
                printer_events.on_progress_update(
                    self.printer_id, status.progress_percent, None, None, remaining_min,
                )
                self._last_progress = status.progress_percent
                self._last_progress_write = now

        # ----------------------------------------------------------
        # 3. Telemetry + heartbeat (throttled)
        # ----------------------------------------------------------
        live_state_allowed = True
        if self._marked_offline or time.time() - self._last_heartbeat >= HEARTBEAT_INTERVAL:
            live_state_allowed = False
            try:
                with engine.begin() as conn:
                    bed_t = status.bed_temp
                    bed_tt = status.bed_target
                    noz_t = status.nozzle_temp
                    noz_tt = status.nozzle_target
                    gstate = status.internal_state
                    progress = status.progress_percent
                    fan_speed_val = None  # not part of the documented status endpoints

                    if status.state == OctoPrintState.PRINTING:
                        stage = "Printing"
                    elif status.state == OctoPrintState.PAUSED:
                        stage = "Paused"
                    elif status.state == OctoPrintState.ERROR:
                        stage = "Error"
                    elif noz_tt and noz_tt > 0:
                        stage = "Heating hotend"
                    elif bed_tt and bed_tt > 0:
                        stage = "Heating bed"
                    else:
                        stage = "Idle"

                    conn.execute(
                        text(f"UPDATE printers SET last_seen={sql.now()},"  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                        " bed_temp=COALESCE(:bed_t,bed_temp),bed_target_temp=COALESCE(:bed_tt,bed_target_temp),"
                        " nozzle_temp=COALESCE(:noz_t,nozzle_temp),nozzle_target_temp=COALESCE(:noz_tt,nozzle_target_temp),"
                        " gcode_state=COALESCE(:gstate,gcode_state),print_stage=COALESCE(:stage,print_stage),"
                        " fan_speed=COALESCE(:fan_speed,fan_speed) WHERE id=:pid"),
                        {"bed_t": bed_t, "bed_tt": bed_tt, "noz_t": noz_t, "noz_tt": noz_tt,
                         "gstate": gstate, "stage": stage, "fan_speed": fan_speed_val, "pid": self.printer_id}
                    )

                    if gstate in ('PRINTING', 'PAUSED') and time.time() - self._last_telemetry_insert >= 60:
                        self._last_telemetry_insert = time.time()
                        try:
                            conn.execute(
                                text("INSERT INTO printer_telemetry (printer_id, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed) VALUES (:pid, :bed_t, :noz_t, :bed_tt, :noz_tt, :fan)"),
                                {"pid": self.printer_id, "bed_t": bed_t, "noz_t": noz_t, "bed_tt": bed_tt, "noz_tt": noz_tt, "fan": fan_speed_val}
                            )
                            conn.execute(text(f"DELETE FROM printer_telemetry WHERE recorded_at < {sql.now_offset('-90 days')}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                        except Exception as e:
                            log.debug(f"[{self.name}] Telemetry insert: {e}")

                live_state_allowed = True
                self._last_heartbeat = time.time()
                self._marked_offline = False
                self._offline_written = False

                payload = {
                    'printer_id': self.printer_id, 'bed_temp': bed_t, 'bed_target': bed_tt,
                    'nozzle_temp': noz_t, 'nozzle_target': noz_tt, 'state': gstate,
                    'progress': progress, 'remaining_min': remaining_min,
                    'current_layer': None, 'total_layers': None,
                    'fan_speed': fan_speed_val,
                }
                try:
                    ws_push('printer_telemetry', payload)
                except Exception:
                    log.warning("Committed telemetry WebSocket delivery failed")
                if _mqtt_republish:
                    try:
                        _mqtt_republish.republish_telemetry(self.printer_id, self.name,
                            {key: value for key, value in payload.items() if key != 'printer_id'})
                    except Exception:
                        log.warning("Committed telemetry MQTT delivery failed")

            except Exception:
                log.warning("Telemetry persistence failed; retry on next observation")

        # Optional evidence stays separate and after a successful status commit.
        if live_state_allowed:
            from modules.printers.monitors.live_state import observe_status
            observe_status(engine, self.printer_id, status, 'octoprint', owner=self)


# ------------------------------------------------------------------
# Daemon wiring (license retirement, startup)
# ------------------------------------------------------------------
def _credentials(api_key_raw):
    """The OctoPrint application key from the stored, usually encrypted, credential."""
    if not api_key_raw:
        return ""
    try:
        from core.crypto import decrypt
        return decrypt(api_key_raw)
    except Exception:
        # Not encrypted or decrypt failed: use it as the raw key.
        return api_key_raw


def _rows():
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id, name, api_host, api_key FROM printers WHERE api_type='octoprint' AND api_host IS NOT NULL AND is_active IS TRUE")
        ).mappings().fetchall()


def _inventory():
    """{printer_id: (fingerprint, (name, host, api_key))} for license retirement (B2c)."""
    from core.license_monitor import connection_fingerprint
    inventory = {}
    for row in _rows():
        api_key = _credentials(row["api_key"] or "")
        inventory[row["id"]] = (connection_fingerprint("octoprint", row["api_host"], api_key),
                                (row["name"], row["api_host"], api_key))
    return inventory


def _session(printer_id, name, host, api_key):
    log.info(f"Starting OctoPrint monitor for {name} ({host})")
    return OctoPrintMonitorThread(printer_id=printer_id, name=name, host=host, api_key=api_key)


def start_octoprint_monitors():
    """Load OctoPrint printers from DB and start monitor threads. License fleet lock off."""
    from core.monitor_bootstrap import bootstrap_monitor_authority
    bootstrap_monitor_authority()
    threads = []
    try:
        for row in _rows():
            t = OctoPrintMonitorThread(
                printer_id=row["id"], name=row["name"], host=row["api_host"],
                api_key=_credentials(row["api_key"] or ""),
            )
            t.start()
            threads.append(t)
            log.info(f"Started OctoPrint monitor for {row['name']} ({row['api_host']})")
    except Exception as e:
        log.error(f"Failed to start OctoPrint monitors: {e}")
    return threads


if __name__ == "__main__":
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    from core.schema.bootstrap import configure_orm
    configure_orm()
    from core.job_timeline import start
    start("octoprint_monitor")
    import signal

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    from core.interfaces.filament_ownership import log_external_filament_accounting
    log_external_filament_accounting(log)

    shutdown = threading.Event()

    def _shutdown(signum, frame):
        log.info(f"Received signal {signum}, shutting down OctoPrint monitors...")
        shutdown.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # License fleet lock enabled: restart retirement (B2c, core.license_monitor).
    # Otherwise the monitors start once (retried every 60 s while none exist), as before.
    from core.license_monitor import run_monitor_process
    run_monitor_process(engine, _inventory, _session, start_octoprint_monitors, 'OctoPrint', shutdown, log)
    log.info("OctoPrint monitor daemon stopped.")
