"""
PrinterMonitor — single-printer MQTT connection manager.

Handles connect/disconnect, MQTT state merging, telemetry DB writes,
progress tracking, spool level checks, and state-transition dispatch.
Job lifecycle and telemetry parsing helpers are in mqtt_job_lifecycle.py
and mqtt_telemetry.py respectively.
"""

import os
import sys
sys.path.insert(0, os.environ.get('BACKEND_PATH', '/app/backend'))

import json
import logging
import time
from datetime import datetime, timezone
from threading import Thread, Lock
from typing import Dict, Optional, Any

import core.crypto as crypto
from modules.printers.adapters.bambu import BambuPrinter
from core.db_utils import get_db
from modules.printers.monitors.mqtt_telemetry import (
    resolve_stage_label,
    parse_lights,
    parse_ams_env,
    parse_h2d_nozzles,
    parse_h2d_external_spools,
)
from modules.printers.monitors.mqtt_job_lifecycle import (
    record_job_started,
    record_job_ended,
)

try:
    import modules.notifications.mqtt_republish as mqtt_republish
except ImportError:
    mqtt_republish = None
try:
    from core.ws_hub import push_event as ws_push
except ImportError:
    def ws_push(*a, **kw): pass

log = logging.getLogger('mqtt_monitor')


class PrinterMonitor:
    """Monitors a single printer's MQTT stream."""

    def __init__(self, printer_id: int, name: str, ip: str, serial: str, access_code: str):
        self.printer_id = printer_id
        self.name = name
        self.ip = ip
        self.serial = serial
        self.access_code = access_code

        self._bambu: Optional[BambuPrinter] = None
        self._state: Dict[str, Any] = {}
        self._last_gcode_state: Optional[str] = None
        self._current_job_id: Optional[int] = None
        self._linked_job_id: Optional[int] = None  # Linked scheduled job from jobs table
        self._last_progress_update: float = 0
        self._last_spool_check: float = 0
        self._lock = Lock()

    def connect(self) -> bool:
        """Connect to printer MQTT."""
        try:
            self._bambu = BambuPrinter(
                ip=self.ip,
                serial=self.serial,
                access_code=self.access_code,
                on_status_update=self._on_status,
                client_id=f"odin_{self.printer_id}_{int(time.time())}"
            )
            if self._bambu.connect():
                log.info(f"[{self.name}] Connected")
                return True
            else:
                log.error(f"[{self.name}] Connection failed")
                return False
        except Exception as e:
            log.error(f"[{self.name}] Connection error: {e}")
            return False

    def disconnect(self):
        """Disconnect from printer."""
        if self._bambu:
            self._bambu.disconnect()
            self._bambu = None
            log.info(f"[{self.name}] Disconnected")

    def _trigger_reschedule(self):
        """Fire-and-forget POST to /api/scheduler/run so bumped jobs get reassigned."""
        def _do():
            try:
                import urllib.request
                api_key = os.environ.get('API_KEY', '')
                req = urllib.request.Request(
                    'http://localhost:8000/api/scheduler/run',
                    data=b'{}',
                    headers={'Content-Type': 'application/json', 'X-API-Key': api_key},
                    method='POST'
                )
                urllib.request.urlopen(req, timeout=5)
                log.info(f"[{self.name}] Triggered scheduler re-run after bump")
            except Exception as e:
                log.debug(f"[{self.name}] Scheduler trigger failed (non-critical): {e}")
        Thread(target=_do, daemon=True).start()

    def _dispatch_alert(self, alert_type: str, severity: str, title: str,
                        message: str = "", job_id: int = None, spool_id: int = None,
                        metadata: dict = None):
        """
        Create alert records for all users who have this alert type enabled.
        Uses raw SQL to avoid importing SQLAlchemy into the monitor daemon.
        Handles deduplication for spool_low alerts.
        """
        try:
            with get_db() as conn:
                cur = conn.cursor()

                # Get all users with preferences for this alert type
                cur.execute("""
                    SELECT user_id, in_app, browser_push, email
                    FROM alert_preferences
                    WHERE alert_type = ? AND in_app = 1
                """, (alert_type,))
                prefs = cur.fetchall()

                # If no preferences exist, seed defaults for all active users
                if not prefs:
                    cur.execute("SELECT id FROM users WHERE is_active = 1")
                    users = cur.fetchall()
                    defaults = {
                        'print_complete': (1, 0, 0),
                        'print_failed': (1, 1, 0),
                        'spool_low': (1, 0, 0),
                        'maintenance_overdue': (1, 0, 0),
                        'schedule_bump': (1, 0, 0),
                    }
                    for (uid,) in users:
                        for at, (ia, bp, em) in defaults.items():
                            cur.execute("""
                                INSERT OR IGNORE INTO alert_preferences
                                (user_id, alert_type, in_app, browser_push, email, threshold_value)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (uid, at, ia, bp, em, 100.0 if at == 'spool_low' else None))
                    conn.commit()
                    # Re-query
                    cur.execute("""
                        SELECT user_id, in_app, browser_push, email
                        FROM alert_preferences
                        WHERE alert_type = ? AND in_app = 1
                    """, (alert_type,))
                    prefs = cur.fetchall()

                created = 0
                for user_id, in_app, browser_push, email in prefs:
                    # Dedup: spool_low — skip if unread alert exists for same spool
                    if alert_type == 'spool_low' and spool_id:
                        cur.execute("""
                            SELECT 1 FROM alerts
                            WHERE user_id = ? AND alert_type = 'SPOOL_LOW'
                            AND spool_id = ? AND is_read = 0 AND is_dismissed = 0
                            LIMIT 1
                        """, (user_id, spool_id))
                        if cur.fetchone():
                            continue

                    # Create in-app alert
                    cur.execute("""
                        INSERT INTO alerts
                        (user_id, alert_type, severity, title, message,
                         printer_id, job_id, spool_id, metadata_json, is_read, is_dismissed, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
                    """, (user_id, alert_type.upper(), severity.upper(), title, message,
                          self.printer_id, job_id, spool_id,
                          json.dumps(metadata) if metadata else None,
                          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')))
                    created += 1

                conn.commit()
                if created > 0:
                    log.info(f"[{self.name}] Alert dispatched: {alert_type} to {created} users")
        except Exception as e:
            log.error(f"[{self.name}] Failed to dispatch alert: {e}")

    def _on_status(self, status):
        """Handle incoming MQTT status update."""
        import modules.notifications.event_dispatcher as printer_events
        with self._lock:
            raw = status.raw_data.get('print', {})

            # Update telemetry + heartbeat (throttled to every 10 seconds)
            if time.time() - getattr(self, '_last_heartbeat', 0) >= 10:
                try:
                    with get_db() as conn:
                        bed_t = self._state.get('bed_temper')
                        bed_tt = self._state.get('bed_target_temper')
                        noz_t = self._state.get('nozzle_temper')
                        noz_tt = self._state.get('nozzle_target_temper')
                        gstate = self._state.get('gcode_state')
                        stg_cur = self._state.get('stg_cur', -1)
                        stage = resolve_stage_label(stg_cur, gstate)
                        import json as _json
                        hms_raw = self._state.get('hms', [])
                        hms_j = _json.dumps(hms_raw) if hms_raw else None
                        lights_on_raw = parse_lights(self._state.get('lights_report', []))
                        # Check cooldown — don't overwrite if toggled via API within last 20s
                        cooldown_row = conn.execute('SELECT lights_toggled_at FROM printers WHERE id=?', (self.printer_id,)).fetchone()
                        lights_on = lights_on_raw
                        if cooldown_row and cooldown_row[0]:
                            from datetime import datetime as _dt
                            try:
                                if (_dt.utcnow() - _dt.fromisoformat(cooldown_row[0])).total_seconds() < 20:
                                    lights_on = None
                            except Exception:
                                pass
                        noz_type = self._state.get('nozzle_type')
                        noz_dia = self._state.get('nozzle_diameter')
                        if isinstance(noz_dia, str):
                            try:
                                noz_dia = float(noz_dia)
                            except Exception:
                                noz_dia = None
                        fan_speed_val = self._state.get('cooling_fan_speed')
                        conn.execute(
                            "UPDATE printers SET last_seen=datetime('now'),"
                            " bed_temp=?,bed_target_temp=?,nozzle_temp=?,nozzle_target_temp=?,"
                            " gcode_state=?,print_stage=?,hms_errors=?,lights_on=COALESCE(?,lights_on),"
                            " nozzle_type=?,nozzle_diameter=?,fan_speed=COALESCE(?,fan_speed) WHERE id=?",
                            (bed_t, bed_tt, noz_t, noz_tt, gstate, stage,
                             hms_j, lights_on, noz_type, noz_dia, fan_speed_val, self.printer_id))
                        conn.commit()

                        # Auto-detect printer model from MQTT — write once, never overwrite a user-set value
                        raw_pt = self._state.get('printer_type', '')
                        if raw_pt:
                            try:
                                from modules.models_library.threemf_parser import _friendly_printer_name
                                friendly = _friendly_printer_name(raw_pt) or raw_pt
                                conn.execute(
                                    "UPDATE printers SET model = ? WHERE id = ? AND (model IS NULL OR model = '' OR model = 'Unknown')",
                                    (friendly, self.printer_id)
                                )
                                conn.commit()
                            except Exception as e:
                                log.debug(f"[{self.name}] Model auto-detect failed: {e}")

                        # Auto-detect machine_type (H2D, X1C, P1S, etc.) — always update
                        if raw_pt:
                            try:
                                from modules.models_library.threemf_parser import _friendly_printer_name as _fpn
                                detected_type = _fpn(raw_pt) or raw_pt
                                conn.execute(
                                    "UPDATE printers SET machine_type = ? WHERE id = ? AND (machine_type IS NULL OR machine_type != ?)",
                                    (detected_type, self.printer_id, detected_type)
                                )
                                conn.commit()
                            except Exception as e:
                                log.debug(f"[{self.name}] machine_type detect failed: {e}")

                        # Republish telemetry to external broker
                        if mqtt_republish:
                            try:
                                mqtt_republish.republish_telemetry(self.printer_id, self.name, {
                                    "bed_temp": bed_t, "bed_target": bed_tt,
                                    "nozzle_temp": noz_t, "nozzle_target": noz_tt,
                                    "state": gstate,
                                    "progress": self._state.get('mc_percent'),
                                    "remaining_min": self._state.get('mc_remaining_time'),
                                    "current_layer": self._state.get('layer_num'),
                                    "total_layers": self._state.get('total_layer_num'),
                                })
                            except Exception:
                                pass
                        self._last_heartbeat = time.time()

                        # ---- Timeseries Telemetry Capture ----
                        if gstate in ('RUNNING', 'PREPARE', 'PAUSE') and time.time() - getattr(self, '_last_telemetry_insert', 0) >= 60:
                            self._last_telemetry_insert = time.time()
                            try:
                                conn.execute(
                                    "INSERT INTO printer_telemetry (printer_id, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed) VALUES (?, ?, ?, ?, ?, ?)",
                                    (self.printer_id, bed_t, noz_t, bed_tt, noz_tt, fan_speed_val)
                                )
                                conn.execute("DELETE FROM printer_telemetry WHERE recorded_at < datetime('now', '-90 days')")
                                conn.commit()
                            except Exception as e:
                                log.debug(f"[{self.name}] Telemetry insert: {e}")

                        # ---- AMS Environmental Data Capture ----
                        if time.time() - getattr(self, '_last_ams_env', 0) >= 300:
                            self._last_ams_env = time.time()
                            try:
                                ams_raw_env = self._state.get('ams', {})
                                for entry in parse_ams_env(ams_raw_env):
                                    conn.execute(
                                        "INSERT INTO ams_telemetry (printer_id, ams_unit, humidity, temperature) VALUES (?, ?, ?, ?)",
                                        (self.printer_id, entry['unit_idx'], entry['humidity'], entry['temperature'])
                                    )
                                conn.execute(
                                    "DELETE FROM ams_telemetry WHERE recorded_at < datetime('now', '-90 days')"
                                )
                                conn.commit()
                            except Exception as e:
                                log.debug(f"[{self.name}] AMS env capture: {e}")

                    # ---- H2D Dual-Nozzle / External Spool Parsing ----
                    h2d_nozzle_data = None
                    external_spools = None
                    try:
                        with get_db() as h2d_conn:
                            mt_row = h2d_conn.execute('SELECT machine_type FROM printers WHERE id=?', (self.printer_id,)).fetchone()
                            machine_type = mt_row[0] if mt_row else None
                    except Exception:
                        machine_type = None

                    if machine_type == 'H2D':
                        h2d_nozzle_data = parse_h2d_nozzles(self._state)
                        ams_raw = self._state.get('ams', {})
                        external_spools = parse_h2d_external_spools(ams_raw)

                    # Push telemetry to WebSocket clients
                    ws_payload = {
                        'printer_id': self.printer_id,
                        'bed_temp': bed_t,
                        'bed_target': bed_tt,
                        'nozzle_temp': noz_t,
                        'nozzle_target': noz_tt,
                        'state': gstate,
                        'progress': self._state.get('mc_percent'),
                        'remaining_min': self._state.get('mc_remaining_time'),
                        'current_layer': self._state.get('layer_num'),
                        'total_layers': self._state.get('total_layer_num'),
                        'gcode_file': self._state.get('subtask_name') or self._state.get('gcode_file'),
                    }
                    if h2d_nozzle_data:
                        ws_payload['h2d_nozzles'] = h2d_nozzle_data
                    if external_spools:
                        ws_payload['external_spools'] = external_spools
                    ws_push('printer_telemetry', ws_payload)

                    # Process HMS errors through universal handler for alerts
                    if hms_raw:
                        import json as _json2
                        hms_key = _json2.dumps(hms_raw)
                        if hms_key != getattr(self, '_last_hms_key', None):
                            self._last_hms_key = hms_key
                            try:
                                with get_db() as hconn:
                                    parsed = printer_events.parse_hms_errors(hms_raw)
                                    for err in parsed:
                                        hconn.execute(
                                            "INSERT INTO hms_error_history (printer_id, code, message, severity, source) VALUES (?, ?, ?, ?, ?)",
                                            (self.printer_id, err.get('code', ''), err.get('message', ''), err.get('severity', 'warning'), 'bambu_hms')
                                        )
                                    hconn.execute("DELETE FROM hms_error_history WHERE occurred_at < datetime('now', '-90 days')")
                                    hconn.commit()
                            except Exception as e:
                                log.debug(f"[{self.name}] HMS history insert: {e}")
                        printer_events.process_hms_errors(self.printer_id, hms_raw)

                except Exception as e:
                    log.warning(f"Failed to update telemetry for printer {self.printer_id}: {e}")

            # Check for camera URL auto-discovery (only X1C/H2D broadcast rtsp_url)
            ipcam = raw.get('ipcam', {})
            rtsp_url = ipcam.get('rtsp_url')
            if rtsp_url:
                full_url = f"rtsps://bblp:{self.access_code}@{self.ip}:322/streaming/live/1"
                printer_events.discover_camera(self.printer_id, full_url)

            # Merge partial updates into state
            for key, value in raw.items():
                if value is not None:
                    self._state[key] = value

            # Update progress if we have an active job (throttled to every 5 seconds)
            if self._current_job_id and time.time() - self._last_progress_update >= 5:
                self._update_progress()

            # Check AMS spool levels (throttled to every 60 seconds)
            if time.time() - self._last_spool_check >= 60:
                self._check_spool_levels()
                self._last_spool_check = time.time()

            # Check for state transitions
            gcode_state = self._state.get('gcode_state')
            if gcode_state and gcode_state != self._last_gcode_state:
                self._on_state_change(self._last_gcode_state, gcode_state)
                self._last_gcode_state = gcode_state

    def _check_spool_levels(self):
        """Check AMS spool remaining weights and fire spool_low alerts."""
        ams_data = self._state.get('ams', {})
        if not ams_data:
            return

        # Get threshold from first user's preference (they all share the same default)
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT threshold_value FROM alert_preferences
                    WHERE alert_type = 'spool_low' AND threshold_value IS NOT NULL
                    LIMIT 1
                """)
                row = cur.fetchone()
                threshold = row[0] if row else 100.0
        except Exception:
            threshold = 100.0

        # Check filament slots for this printer
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT fs.slot_number, fs.assigned_spool_id, s.remaining_weight_g,
                           fl.brand, fl.name as filament_name
                    FROM filament_slots fs
                    LEFT JOIN spools s ON fs.assigned_spool_id = s.id
                    LEFT JOIN filament_library fl ON s.filament_id = fl.id
                    WHERE fs.printer_id = ? AND fs.assigned_spool_id IS NOT NULL
                """, (self.printer_id,))

                for slot_num, spool_id, remaining, brand, fil_name in cur.fetchall():
                    if remaining is not None and remaining < threshold and remaining > 0:
                        spool_label = f"{brand} {fil_name}" if brand and fil_name else f"Spool #{spool_id}"
                        self._dispatch_alert(
                            alert_type='spool_low',
                            severity='warning',
                            title=f"Low Spool: {spool_label} ({self.name} slot {slot_num})",
                            message=f"{remaining:.0f}g remaining (threshold: {threshold:.0f}g)",
                            spool_id=spool_id,
                            metadata={"remaining_g": remaining, "threshold_g": threshold, "slot": slot_num}
                        )
        except Exception as e:
            log.error(f"[{self.name}] Spool level check failed: {e}")

    def _on_state_change(self, old_state: Optional[str], new_state: str):
        """Handle print state transitions."""
        log.info(f"[{self.name}] State: {old_state} -> {new_state}")

        # Entering RUNNING or PAUSE without a tracked job = new job
        if new_state in ('RUNNING', 'PAUSE') and not self._current_job_id:
            if old_state in (None, 'IDLE', 'FINISH', 'FAILED', 'PREPARE', 'PAUSE'):
                self._job_started()

        # RUNNING/PAUSE -> FINISH = Job completed
        elif new_state == 'FINISH' and old_state in ('RUNNING', 'PAUSE'):
            self._job_ended('completed')

        # RUNNING/PAUSE -> FAILED = Job failed
        elif new_state == 'FAILED' and old_state in ('RUNNING', 'PAUSE'):
            self._job_ended('failed')

        # RUNNING/PAUSE -> IDLE = Job cancelled
        elif new_state == 'IDLE' and old_state in ('RUNNING', 'PAUSE'):
            self._job_ended('cancelled')

        # FINISH/FAILED -> IDLE = printer fully settled, attempt next queued job
        elif new_state == 'IDLE' and old_state in ('FINISH', 'FAILED'):
            Thread(target=self._try_dispatch, daemon=True).start()

    def _job_started(self):
        """Thin wrapper: delegate to mqtt_job_lifecycle.record_job_started()."""
        new_job_id, linked_job_id, start_weights = record_job_started(
            printer_id=self.printer_id,
            printer_name=self.name,
            state=self._state,
            dispatch_alert_fn=self._dispatch_alert,
            trigger_reschedule_fn=self._trigger_reschedule,
        )
        if new_job_id is not None:
            self._current_job_id = new_job_id
            self._linked_job_id = linked_job_id
            self._start_spool_weights = start_weights

    def _update_progress(self):
        """Update progress data for current job."""
        progress = self._state.get('mc_percent')
        remaining = self._state.get('mc_remaining_time')
        current_layer = self._state.get('layer_num')

        # Only update if we have meaningful data
        if progress is None and remaining is None and current_layer is None:
            return

        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE print_jobs SET progress_percent = COALESCE(?, progress_percent), "
                    "remaining_minutes = COALESCE(?, remaining_minutes), "
                    "current_layer = COALESCE(?, current_layer) WHERE id = ?",
                    (progress, remaining, current_layer, self._current_job_id)
                )
                conn.commit()
            self._last_progress_update = time.time()
        except Exception as e:
            log.error(f"[{self.name}] Failed to update progress: {e}")

    def _job_ended(self, status: str):
        """Thin wrapper: delegate to mqtt_job_lifecycle.record_job_ended()."""
        if not self._current_job_id:
            log.warning(f"[{self.name}] Job ended but no current job tracked")
            return

        final_linked_id = record_job_ended(
            printer_id=self.printer_id,
            printer_name=self.name,
            current_job_id=self._current_job_id,
            linked_job_id=self._linked_job_id,
            status=status,
            state=self._state,
            start_spool_weights=getattr(self, '_start_spool_weights', {}),
            dispatch_alert_fn=self._dispatch_alert,
        )
        self._current_job_id = None
        self._linked_job_id = None

    def _try_dispatch(self):
        """Background thread: attempt to dispatch the next queued job after printer goes idle."""
        try:
            # Brief settle delay so the printer is fully in IDLE before we upload
            time.sleep(5)
            import printer_dispatch
            printer_dispatch.attempt_dispatch(self.printer_id)
        except Exception as e:
            log.warning(f"[{self.name}] Dispatch attempt error: {e}")
