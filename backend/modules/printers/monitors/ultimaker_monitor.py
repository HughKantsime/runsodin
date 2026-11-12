"""
Ultimaker Monitor — polling daemon for Ultimaker S-series and Ultimaker 3 (Experimental).

Part 1: monitoring and camera only. Built from Ultimaker's public local API documentation
(see adapters/ultimaker.py). NOT verified on hardware. Nothing here writes to the printer:
the adapter can only send GET.

Architecture:
  - One thread per Ultimaker printer.
  - Polls GET /api/v1/printer and GET /api/v1/print_job every POLL_INTERVAL seconds.
  - Feeds the same shared handlers the OctoPrint monitor uses: the event_dispatcher
    on_print_* functions (job tracking, start/end observation) and live_state.observe_status.
  - Fail closed: an UNKNOWN reading is recorded as such, never advances last_seen and never starts
    or ends a print. A print is recorded as completed only when the printer reports the job as
    finished (wait_cleanup) at 100 %; otherwise it is recorded as cancelled.
  - OFFLINE is persisted after MAX_CONSECUTIVE_FAILURES unanswered polls in a row; the next
    answer writes the real state at once.
  - No dispatch is attempted after a print: job sending is not available for this type yet.
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

from modules.printers.adapters.ultimaker import UltimakerPrinter, UltimakerState

log = logging.getLogger(__name__)

POLL_INTERVAL = 10           # seconds between polls
HEARTBEAT_INTERVAL = 10      # seconds between telemetry + last_seen writes
PROGRESS_DB_INTERVAL = 5     # at most one progress write per this many seconds
MAX_CONSECUTIVE_FAILURES = 3  # unanswered polls in a row before OFFLINE is persisted

_ODIN_STATE = {"PRINTING": "RUNNING", "PAUSED": "PAUSE", "ERROR": "FAILED"}
_ACTIVE = ("PRINTING", "PAUSED")


class UltimakerMonitorThread(threading.Thread):
    """Monitor thread for a single Ultimaker printer."""

    def __init__(self, printer_id: int, name: str, host: str, port: int = 80):
        super().__init__(daemon=True)
        self.printer_id = printer_id
        self.name = name
        self.host = host
        self.client = UltimakerPrinter(host=host, port=port)
        self._running = True
        self._consecutive_failures = 0
        self._last_heartbeat = 0
        self._last_state = None
        self._last_filename = None
        self._job_uuid = None
        self._last_progress = 0.0
        self._last_progress_write = 0
        self._last_telemetry_insert = 0
        self._marked_offline = False
        self._offline_written = False  # OFFLINE is in the row now (distinct from the heartbeat flag)
        self._gap_state = None
        self._status_lock = threading.RLock()
        self._retired = False

    def stop(self):
        with self._status_lock:
            self._retired = True
            self._running = False

    def run(self):
        log.info(f"[{self.name}] Ultimaker monitor started for {self.host}")
        self._register_camera()
        self._log_identity()
        while self._running:
            try:
                status = self.client.get_status()
                if status.state == UltimakerState.DISCONNECTED:
                    # No usable answer: count it as a failed poll so OFFLINE is only
                    # persisted after MAX_CONSECUTIVE_FAILURES in a row.
                    raise ConnectionError("Ultimaker status poll failed")
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.info(f"[{self.name}] Connection recovered, re-registering camera...")
                    self._register_camera()
                self._consecutive_failures = 0
                self._process_status(status)
            except Exception:
                self._consecutive_failures += 1
                with self._status_lock:
                    self._gap_state = None  # a later UNKNOWN reading must be written again
                    if not self._retired:
                        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self._offline_written:
                            if self._persist_state("OFFLINE", "Offline"):
                                self._marked_offline = True
                                self._offline_written = True
                log.warning("Printer poll failed (%s consecutive failures)", self._consecutive_failures)
            time.sleep(POLL_INTERVAL)
        log.info(f"[{self.name}] Ultimaker monitor stopped")

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

    def _log_identity(self):
        """Log the printer's own variant and firmware (GET /system); best effort."""
        try:
            info = self.client.get_system()
            if info:
                log.info("[%s] %s, firmware %s", self.name, info["variant"] or "Ultimaker", info["firmware"] or "unknown")
        except Exception:
            log.debug("Ultimaker identity read failed for printer %s", self.printer_id)

    def _register_camera(self):
        """Register the documented default stream URL (port 8080) through camera discovery.

        Discovery applies the usual policy: only when the printer has the camera enabled and no
        camera URL yet, the host equals the configured host, and the device destination policy
        (private-address check) passes. Nothing is probed."""
        import modules.notifications.event_dispatcher as printer_events
        try:
            cam_url = self.client.default_camera_url()
            if cam_url:
                printer_events.discover_camera(self.printer_id, cam_url)
        except Exception:
            log.warning("Camera registration failed for printer %s", self.printer_id)

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
        if status.state == UltimakerState.UNKNOWN:
            gstate = status.internal_state
            if self._gap_state != gstate:
                if self._persist_state(gstate, "Unknown"):
                    self._gap_state = gstate
                    self._marked_offline = True  # the next real reading writes its heartbeat at once
                    self._offline_written = False
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

        # A different job uuid while a print was already running or paused: the earlier job ended
        # between two polls. Record it as cancelled (its result was not seen) and start the new one.
        if (current_state in _ACTIVE and self._last_state in _ACTIVE and self._job_uuid
                and status.job_uuid and status.job_uuid != self._job_uuid):
            printer_events.on_print_cancelled(self.printer_id, self._last_filename or "Unknown")
            self._last_state = None
            first_look = False

        if current_state != self._last_state:
            previous = self._last_state
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
                    physical_job_id=f"ultimaker_{status.job_uuid}" if status.job_uuid else None,
                )
            elif odin_state == "IDLE" and previous in _ACTIVE:
                # Completed needs the printer's own finished state (wait_cleanup) at 100 %. A job
                # that vanished, or was aborted (cleanup reached short of 100 %), is cancelled.
                finished = (status.finished and status.progress_percent is not None
                            and status.progress_percent >= 100.0)
                name = current_file or self._last_filename or "Unknown"
                if finished:
                    printer_events.on_print_complete(self.printer_id, name, status.time_printing, None)
                else:
                    printer_events.on_print_cancelled(self.printer_id, name)
                self._job_uuid = None
            elif odin_state == "FAILED":
                error_msg = "Printer reported error state"
                # An error with no print running ends nothing; the error itself is still recorded.
                if previous in _ACTIVE:
                    printer_events.on_print_failed(
                        self.printer_id,
                        current_file or self._last_filename or "Unknown",
                        error_msg,
                    )
                    self._job_uuid = None
                try:
                    printer_events.record_error(
                        self.printer_id,
                        error_code=f"ULTIMAKER_{current_state}",
                        error_message=error_msg,
                        source="ultimaker",
                        severity="error",
                        history=True,
                    )
                except Exception as e:
                    log.debug(f"Failed to record Ultimaker error: {e}")
            elif odin_state == "PAUSE":
                printer_events.on_print_paused(self.printer_id, current_file)

            self._last_state = current_state

        if current_file:
            self._last_filename = current_file
        if current_state in _ACTIVE and status.job_uuid:
            self._job_uuid = status.job_uuid

        # ----------------------------------------------------------
        # 2. Progress (while printing): at most one write per PROGRESS_DB_INTERVAL
        # ----------------------------------------------------------
        if status.state == UltimakerState.PRINTING and status.progress_percent:
            now = time.time()
            if (abs(status.progress_percent - self._last_progress) >= 0.1
                    and now - self._last_progress_write >= PROGRESS_DB_INTERVAL):
                printer_events.on_progress_update(
                    self.printer_id, status.progress_percent, None, None, remaining_min,
                )
                self._last_progress = status.progress_percent
                self._last_progress_write = now

        # ----------------------------------------------------------
        # 3. Telemetry + heartbeat (throttled). The first extruder is the nozzle: the existing row,
        #    telemetry table and websocket payload have no place for a second one.
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

                    if status.state == UltimakerState.PRINTING:
                        stage = "Preparing" if status.job_state == "pre_print" else "Printing"
                    elif status.state == UltimakerState.PAUSED:
                        stage = "Paused"
                    elif status.state == UltimakerState.ERROR:
                        stage = "Error"
                    elif status.finished:
                        stage = "Finished"
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
            observe_status(engine, self.printer_id, status, 'ultimaker', owner=self)


# ------------------------------------------------------------------
# Daemon wiring (license retirement, startup)
# ------------------------------------------------------------------
def _rows():
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id, name, api_host FROM printers WHERE api_type='ultimaker' AND api_host IS NOT NULL AND is_active IS TRUE")
        ).mappings().fetchall()


def _inventory():
    """{printer_id: (fingerprint, (name, host))} for license retirement (B2c)."""
    from core.license_monitor import connection_fingerprint
    inventory = {}
    for row in _rows():
        inventory[row["id"]] = (connection_fingerprint("ultimaker", row["api_host"]),
                                (row["name"], row["api_host"]))
    return inventory


def _session(printer_id, name, host):
    log.info(f"Starting Ultimaker monitor for {name} ({host})")
    return UltimakerMonitorThread(printer_id=printer_id, name=name, host=host)


def start_ultimaker_monitors():
    """Load Ultimaker printers from DB and start monitor threads. License fleet lock off."""
    from core.monitor_bootstrap import bootstrap_monitor_authority
    bootstrap_monitor_authority()
    threads = []
    try:
        for row in _rows():
            t = UltimakerMonitorThread(printer_id=row["id"], name=row["name"], host=row["api_host"])
            t.start()
            threads.append(t)
            log.info(f"Started Ultimaker monitor for {row['name']} ({row['api_host']})")
    except Exception as e:
        log.error(f"Failed to start Ultimaker monitors: {e}")
    return threads


if __name__ == "__main__":
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    from core.schema.bootstrap import configure_orm
    configure_orm()
    from core.job_timeline import start
    start("ultimaker_monitor")
    import signal

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    from core.interfaces.filament_ownership import log_external_filament_accounting
    log_external_filament_accounting(log)

    shutdown = threading.Event()

    def _shutdown(signum, frame):
        log.info(f"Received signal {signum}, shutting down Ultimaker monitors...")
        shutdown.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # License fleet lock enabled: restart retirement (B2c, core.license_monitor).
    # Otherwise the monitors start once (retried every 60 s while none exist), as before.
    from core.license_monitor import run_monitor_process
    run_monitor_process(engine, _inventory, _session, start_ultimaker_monitors, 'Ultimaker', shutdown, log)
    log.info("Ultimaker monitor daemon stopped.")
