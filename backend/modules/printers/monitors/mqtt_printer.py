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
from urllib.parse import quote as urlquote
from datetime import datetime, timezone
from threading import Thread, Lock, Event
from typing import Dict, Optional, Any

import core.crypto as crypto
from modules.printers.adapters.bambu import BambuPrinter
from core.db_utils import get_db
from core.db_compat import sql
from modules.printers.monitors.mqtt_telemetry import (
    resolve_stage_label,
    parse_lights,
    parse_ams_env,
    parse_h2d_nozzles,
    h2d_extruder_temperatures,
    finite_nozzle_measurement,
    parse_bambu_speed,
    parse_h2d_external_spools,
    ota_product_name,
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


# Failed AMS inventory syncs retry on later heartbeats with exponential backoff.
AMS_SYNC_MAX_ATTEMPTS = 5
AMS_SYNC_RETRY_BASE_SECONDS = 10
AMS_SYNC_RETRY_MAX_SECONDS = 300

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
        self._camera_discovered: bool = False
        self._lock = Lock()
        self._status_received = Event()
        self._pending_running_state = None
        self._pending_hms_terminal = None
        self._connection_generation = 0
        self._connection_retired = False
        self._transport_offline = False

    def connect(self) -> bool:
        """Connect to printer MQTT.

        Behind `ODIN_TELEMETRY_V2=1`, the monitor uses V2's
        BambuTelemetryAdapter but dumps the parsed section back to a
        dict so the existing _on_status() domain logic — which reads
        `status.raw_data['print']` — continues to work unchanged.
        """
        from modules.printers.telemetry.feature_flag import is_v2_enabled

        self._status_received.clear()
        try:
            if is_v2_enabled():
                return self._connect_v2()
            generation = self._begin_connection()
            self._bambu = BambuPrinter(
                ip=self.ip,
                serial=self.serial,
                access_code=self.access_code,
                on_status_update=lambda status: self._on_status(status, generation),
                client_id=f"odin_{self.printer_id}_{int(time.time())}"
            )
            if self._bambu.connect():
                log.info(f"[{self.name}] Connected")
                self._recover_orphaned_jobs()
                return True
            else:
                log.error(f"[{self.name}] Connection failed")
                return False
        except Exception as e:
            log.error(f"[{self.name}] Connection error: {e}")
            return False

    def _connect_v2(self) -> bool:
        """V2 path — BambuTelemetryAdapter with a shim that mimics the
        legacy PrinterStatus just enough for _on_status to read
        raw_data['print']."""
        from modules.printers.telemetry.bambu.adapter import (
            BambuAdapterConfig,
            BambuTelemetryAdapter,
        )
        from modules.printers.telemetry.bambu.broker_policy import (
            resolve_bambu_broker_config,
        )
        from modules.printers.telemetry.events import BambuInfoEvent, BambuReportEvent, ConnectionEvent
        generation = self._begin_connection()

        class _LegacyStatusShim:
            """Minimal legacy-PrinterStatus shape. Only raw_data is read."""
            def __init__(self, section_dict, mqtt_retained):
                self.raw_data = {"print": section_dict}
                self.mqtt_retained = mqtt_retained

        on_status = self._on_status

        def emit(item):
            if isinstance(item, BambuReportEvent):
                # model_dump() preserves the same keys legacy saw
                section_dict = item.section.model_dump(exclude_unset=True)
                on_status(_LegacyStatusShim(section_dict, item.mqtt_retained), generation)
            elif isinstance(item, BambuInfoEvent):
                # Model identity (ota product_name); persisted on the next heartbeat.
                product = ota_product_name(item.section.model_dump())
                if product:
                    with self._lock:
                        if generation == self._connection_generation:
                            self._reported_model = product
            elif isinstance(item, ConnectionEvent) and item.kind == "connected":
                adapter = getattr(self, '_v2_adapter', None)
                if adapter is not None and hasattr(adapter, 'request_version'):
                    adapter.request_version()
            elif isinstance(item, ConnectionEvent) and item.kind in ("disconnected", "error"):
                with self._lock:
                    if generation != self._connection_generation or self._connection_retired:
                        return
                    self._transport_offline = True
                    try:
                        with get_db() as conn:
                            conn.execute("UPDATE printers SET gcode_state='OFFLINE',print_stage='Offline' WHERE id=?", (self.printer_id,))
                            conn.commit()
                    except Exception:
                        log.warning("Offline state persistence failed; retry on next connection check")

        endpoint = resolve_bambu_broker_config(
            self.ip, printer_id=f"monitor-{self.printer_id}",
        )
        config = BambuAdapterConfig(
            printer_id=f"monitor-{self.printer_id}",
            serial=self.serial,
            host=endpoint.host,
            port=endpoint.port,
            use_tls=endpoint.use_tls,
            access_code=self.access_code,
        )
        self._v2_adapter = BambuTelemetryAdapter(config, emitter=emit)
        try:
            self._v2_adapter.start()
            log.info(f"[{self.name}] Connected (V2)")
            self._recover_orphaned_jobs()
            return True
        except Exception as e:
            log.error(f"[{self.name}] V2 connection error: {e}")
            return False

    def _recover_orphaned_jobs(self):
        """Close any 'running' jobs in the DB if the printer is not actually printing."""
        # Wait before taking a lock or opening a transaction needed by callbacks.
        self._status_received.wait(3)
        try:
            with self._lock, get_db() as conn:
                cur = conn.cursor()
                rows = cur.execute(
                    "SELECT id, job_name, started_at, filename, scheduled_job_id FROM print_jobs "
                    "WHERE printer_id = ? AND status = 'running' ORDER BY id",
                    (self.printer_id,)
                ).fetchall()
                if not rows:
                    return
                gcode_state = self._state.get('gcode_state', '')
                from core.registry import registry

                policy = registry.get_provider("EducationPolicyProvider")
                from core.dispatch_observations import generic_observation_reserved,prepare_generic_observation
                incoming_name=self._state.get('subtask_name')
                incoming=policy.classify_monitor_observation(conn,printer_id=self.printer_id,observed_filename=incoming_name) if policy else None
                education_sensitive=bool(incoming and (incoming.get('education_owned') or incoming.get('education_reserved')))
                if generic_observation_reserved(conn,printer_id=self.printer_id,observed_filename=incoming_name):
                    # Unresolved physical attempts cannot be closed from idle or an unrelated task.
                    if not education_sensitive and gcode_state in ('RUNNING','PAUSE'):
                        identifier=prepare_generic_observation(conn,printer_id=self.printer_id,observed_filename=incoming_name)
                        if identifier is not None:
                            self._current_job_id=identifier
                            self._linked_job_id=conn.execute('SELECT scheduled_job_id FROM print_jobs WHERE id=?',(identifier,)).fetchone()[0]
                            from modules.printers.monitors.mqtt_job_lifecycle import load_mqtt_start_weights
                            self._start_spool_weights=load_mqtt_start_weights(conn,identifier)
                    conn.commit()
                    log.info('[%s] Retained dispatch evidence protected during recovery',self.name)
                    return
                if gcode_state in ('RUNNING', 'PAUSE'):
                    # Printer is actually printing — resume tracking the most recent job
                    latest = rows[-1]
                    classification = (
                        policy.classify_monitor_observation(
                            conn,
                            printer_id=self.printer_id,
                            print_job_id=latest[0],
                            observed_filename=latest[1] or latest[3],
                        )
                        if policy
                        else None
                    )
                    if classification and (
                        classification.get("education_owned")
                        or classification.get("education_reserved")
                    ):
                        claimed = policy.claim_monitor_observation(
                            conn,
                            print_job_id=latest[0],
                            printer_id=self.printer_id,
                            observed_filename=latest[1] or latest[3],
                        )
                        if claimed.get("authorized") or (
                            claimed.get("education_reserved")
                            and not claimed.get("education_owned")
                        ):
                            self._current_job_id = latest[0]
                            self._linked_job_id = claimed.get("job_id")
                            log.info(
                                "[%s] Resumed Education/quarantined observation %s",
                                self.name,
                                latest[0],
                            )
                    else:
                        self._current_job_id = latest[0]
                        self._linked_job_id = latest[4]
                        log.info(f"[{self.name}] Resumed tracking job {latest[0]} ({latest[1]})")
                    # Close any older orphaned jobs
                    for row in rows[:-1]:
                        classification = (
                            policy.classify_monitor_observation(
                                conn,
                                printer_id=self.printer_id,
                                print_job_id=row[0],
                                observed_filename=row[1] or row[3],
                            )
                            if policy
                            else None
                        )
                        if classification and (
                            classification.get("education_owned")
                            or classification.get("education_reserved")
                        ):
                            log.error(
                                "[%s] Refusing ambiguous Education orphan closure for %s",
                                self.name,
                                row[0],
                            )
                            continue
                        cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
                            f"UPDATE print_jobs SET status = 'cancelled', ended_at = {sql.now()} WHERE id = ?",
                            (row[0],))
                        log.info(f"[{self.name}] Closed stale duplicate job {row[0]} ({row[1]})")
                else:
                    # Printer is idle — close all orphaned jobs
                    for row in rows:
                        classification = (
                            policy.classify_monitor_observation(
                                conn,
                                printer_id=self.printer_id,
                                print_job_id=row[0],
                                observed_filename=row[1] or row[3],
                            )
                            if policy
                            else None
                        )
                        if classification and (
                            classification.get("education_owned")
                            or classification.get("education_reserved")
                        ):
                            terminal_status = {
                                "FINISH": "completed",
                                "FAILED": "failed",
                            }.get(gcode_state)
                            if terminal_status:
                                result = policy.terminal_monitor_observation(
                                    conn,
                                    print_job_id=row[0],
                                    printer_id=self.printer_id,
                                    terminal_status=terminal_status,
                                )
                                log.info(
                                    "[%s] Education orphan terminal result for %s: %s",
                                    self.name,
                                    row[0],
                                    result,
                                )
                            else:
                                log.error(
                                    "[%s] Ambiguous state %s; Education orphan %s left unchanged",
                                    self.name,
                                    gcode_state or "IDLE",
                                    row[0],
                                )
                            continue
                        cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
                            f"UPDATE print_jobs SET status = 'completed', ended_at = {sql.now()} WHERE id = ?",
                            (row[0],))
                        log.info(f"[{self.name}] Closed orphaned job {row[0]} ({row[1]}) — printer is {gcode_state or 'idle'}")
                conn.commit()
        except Exception as e:
            log.error(f"[{self.name}] Orphaned job recovery failed: {e}")

    def _begin_connection(self):
        with self._lock:
            self._connection_generation += 1
            self._connection_retired = False
            # A held AMS list belongs to the previous connection; the printer
            # re-reports it after pushall.
            self._reset_pending_ams()
            return self._connection_generation

    def _reset_pending_ams(self, report=None):
        self._pending_ams_report = report
        self._ams_sync_failures = 0
        self._ams_sync_retry_at = 0.0

    def disconnect(self):
        """Disconnect from printer."""
        with self._lock:
            self._connection_retired = True
            self._connection_generation += 1
        if self._bambu:
            self._bambu.disconnect()
            self._bambu = None
            log.info(f"[{self.name}] Disconnected")
        # V2 path
        v2 = getattr(self, "_v2_adapter", None)
        if v2 is not None:
            v2.stop()
            self._v2_adapter = None
            log.info(f"[{self.name}] Disconnected (V2)")

    def _trigger_reschedule(self):
        """Fire-and-forget POST to /api/scheduler/run so bumped jobs get reassigned."""
        def _do():
            try:
                import urllib.request
                api_key = os.environ.get('API_KEY', '')
                req = urllib.request.Request(  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-request-object.insecure-request-object -- by design — MQTT printer adapter, local LAN only
                    'http://localhost:8000/api/scheduler/run',
                    data=b'{}',
                    headers={'Content-Type': 'application/json', 'X-API-Key': api_key},
                    method='POST'
                )
                urllib.request.urlopen(req, timeout=5)  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- verified safe — connects to admin-configured printer URLs, SSRF-checked at config time
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
                    WHERE alert_type = ? AND in_app IS TRUE
                """, (alert_type,))
                prefs = cur.fetchall()

                # If no preferences exist, seed defaults for all active users
                if not prefs:
                    cur.execute("SELECT id FROM users WHERE is_active IS TRUE")
                    users = cur.fetchall()
                    defaults = {
                        'print_complete': (True, False, False),
                        'print_failed': (True, True, False),
                        'spool_low': (True, False, False),
                        'maintenance_overdue': (True, False, False),
                        'schedule_bump': (True, False, False),
                    }
                    for (uid,) in users:
                        for at, (ia, bp, em) in defaults.items():
                            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
                            cur.execute(f"""
                                {sql.insert_or_ignore_prefix()} alert_preferences
                                (user_id, alert_type, in_app, browser_push, email, threshold_value)
                                VALUES (?, ?, ?, ?, ?, ?){sql.on_conflict_ignore('user_id, alert_type')}
                            """, (uid, at, ia, bp, em, 100.0 if at == 'spool_low' else None))
                    conn.commit()
                    # Re-query
                    cur.execute("""
                        SELECT user_id, in_app, browser_push, email
                        FROM alert_preferences
                        WHERE alert_type = ? AND in_app IS TRUE
                    """, (alert_type,))
                    prefs = cur.fetchall()

                created = 0
                for user_id, in_app, browser_push, email in prefs:
                    # Dedup: spool_low — skip if unread alert exists for same spool
                    if alert_type == 'spool_low' and spool_id:
                        cur.execute("""
                            SELECT 1 FROM alerts
                            WHERE user_id = ? AND alert_type = 'SPOOL_LOW'
                            AND spool_id = ? AND is_read IS FALSE AND is_dismissed IS FALSE
                            LIMIT 1
                        """, (user_id, spool_id))
                        if cur.fetchone():
                            continue

                    # Create in-app alert
                    cur.execute("""
                        INSERT INTO alerts
                        (user_id, alert_type, severity, title, message,
                         printer_id, job_id, spool_id, metadata_json, is_read, is_dismissed, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, FALSE, ?)
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

    def _on_status(self, status, generation=None):
        """Handle incoming MQTT status update."""
        import modules.notifications.event_dispatcher as printer_events
        with self._lock:
            if self._connection_retired or (generation is not None and generation != self._connection_generation):
                return
            raw = status.raw_data.get('print', {})
            from datetime import datetime, timezone
            pending_nozzles = getattr(self, '_pending_right_nozzle', {})
            for key, column, stamp in (
                ('nozzle_temper_1', 'nozzle_1_temp', 'nozzle_1_temp_observed_at'),
                ('nozzle_target_temper_1', 'nozzle_1_target_temp', 'nozzle_1_target_observed_at'),
            ):
                measured = finite_nozzle_measurement(raw.get(key))
                if measured is not None:
                    pending_nozzles[column] = (measured, stamp, datetime.now(timezone.utc))
            # Real H2D firmware reports the second extruder only in
            # device.extruder.info[id=1] (packed current|target<<16).
            second_extruder = h2d_extruder_temperatures(raw).get(1)
            if second_extruder is not None:
                observed = datetime.now(timezone.utc)
                pending_nozzles['nozzle_1_temp'] = (second_extruder[0], 'nozzle_1_temp_observed_at', observed)
                pending_nozzles['nozzle_1_target_temp'] = (second_extruder[1], 'nozzle_1_target_observed_at', observed)
            self._pending_right_nozzle = pending_nozzles
            incoming_ams_report = raw.get('ams')
            if isinstance(incoming_ams_report, dict) and isinstance(incoming_ams_report.get('ams'), list):
                # Held until the next heartbeat write: Bambu sends the AMS list
                # only on change/pushall, usually between heartbeats.
                self._reset_pending_ams(incoming_ams_report['ams'])

            # Merge correlation and lifecycle fields before any HMS processing.
            # A first RUNNING packet may also contain a print-stopping HMS code;
            # the token observation must be claimed/quarantined first.
            speed_fields = parse_bambu_speed(raw)
            for key, value in raw.items():
                if key in ('nozzle_temper_1', 'nozzle_target_temper_1'):
                    value = finite_nozzle_measurement(value)
                elif key in ('spd_lvl', 'spd_mag'):
                    value = speed_fields['speed_level' if key == 'spd_lvl' else 'speed_percent']
                if value is not None:
                    self._state[key] = value
            gcode_state = self._state.get('gcode_state')
            tracked_before = self._current_job_id
            pending_start_before = self._pending_running_state
            self._status_received.set()
            if gcode_state in ('RUNNING', 'PAUSE') and not self._current_job_id:
                if self._pending_running_state is None:
                    self._pending_running_state = dict(self._state)
            pending = self._pending_running_state
            if pending and not self._current_job_id:
                pending_name = pending.get('subtask_name') or pending.get('gcode_file')
                incoming_name = self._state.get('subtask_name') or self._state.get('gcode_file')
                if pending_name != incoming_name:
                    # Another token cannot erase the unresolved original start.
                    # Keep telemetry, but await evidence for that original token.
                    gcode_state = None
            if pending and not self._current_job_id and gcode_state in ('FINISH', 'FAILED', 'IDLE'):
                # A terminal packet cannot erase a start whose DB transaction
                # failed. Retry the original observed token before ending it.
                incoming = self._state
                pending_name = pending.get('subtask_name') or pending.get('gcode_file')
                incoming_name = incoming.get('subtask_name') or incoming.get('gcode_file')
                if pending_name == incoming_name:
                    try:
                        self._state = pending
                        self._on_state_change(self._last_gcode_state, pending['gcode_state'])
                    finally:
                        self._state = incoming
                    if self._current_job_id:
                        self._last_gcode_state = pending['gcode_state']
                        self._pending_running_state = None
            def transition(value):
                if value and (value != self._last_gcode_state or
                              (value in ('RUNNING', 'PAUSE') and not self._current_job_id)):
                    self._on_state_change(self._last_gcode_state, value)
                    if not (self._pending_running_state and not self._current_job_id):
                        self._last_gcode_state = value

            terminal = gcode_state in ('FINISH', 'FAILED', 'IDLE')
            if not terminal:
                # Initial claim/quarantine must precede HMS authority resolution.
                transition(gcode_state)
            if self._current_job_id:
                self._pending_running_state = None

            def process_hms(packet, expected_print_job_id=None):
                incoming_hms = packet.get('hms')
                if isinstance(incoming_hms, list) and not incoming_hms:
                    self._last_hms_key = None
                    self._last_hms_processed_key = None
                if not incoming_hms:
                    return True
                # HMS belongs to this packet, never an identity from cached telemetry.
                observed_filename = packet.get('subtask_name') or packet.get('gcode_file')
                process_key = (observed_filename, json.dumps(incoming_hms, sort_keys=True))
                if process_key == getattr(self, '_last_hms_processed_key', None):
                    return True
                try:
                    result = printer_events.process_hms_errors(
                        self.printer_id, incoming_hms, observed_filename=observed_filename,
                        expected_print_job_id=expected_print_job_id)
                except Exception:
                    return False
                if isinstance(result, dict) and result.get('retryable') is True:
                    return False
                self._last_hms_processed_key = process_key
                return True

            pending_hms = self._pending_hms_terminal
            if pending_hms is not None and pending_hms['print_job_id'] is None:
                # Only the successful retry of this exact original start can
                # supply missing authority. Never select a latest running row.
                if (tracked_before is None and self._current_job_id is not None
                        and pending_start_before is not None
                        and pending_start_before is pending_hms.get('start')):
                    pending_hms['print_job_id'] = self._current_job_id
            if (pending_hms is not None and pending_hms['print_job_id'] is not None
                    and pending_hms['print_job_id'] != self._current_job_id):
                # A filename can be reused. Old fault evidence cannot mutate a
                # later tracked observation, even if its display name matches.
                self._pending_hms_terminal = None
                pending_hms = None
            resolved = True
            if pending_hms is not None:
                # Untracked prints (no job then, none now) retry under the same
                # no-job identity; otherwise only a bound original job may resolve.
                untracked = pending_hms['print_job_id'] is None and self._current_job_id is None
                resolved = ((pending_hms['print_job_id'] is not None or untracked) and
                            process_hms(pending_hms['packet'], pending_hms['print_job_id']))
                if resolved:
                    if self._current_job_id == pending_hms['print_job_id']:
                        incoming_state = self._state
                        try:
                            self._state = pending_hms['state']
                            transition(pending_hms['terminal'])
                        finally:
                            self._state = incoming_state
                    self._pending_hms_terminal = None
            if resolved:
                resolved = process_hms(raw, self._current_job_id)
                if not resolved and raw.get('hms'):
                    from copy import deepcopy
                    # One bounded observation, not an accumulating event history.
                    self._pending_hms_terminal = {
                        'packet': deepcopy({k: raw[k] for k in
                            ('hms', 'subtask_name', 'gcode_file') if k in raw}),
                        'state': dict(self._state), 'terminal': gcode_state if terminal else None,
                        'print_job_id': self._current_job_id,
                        'start': self._pending_running_state,
                    }
            if terminal and resolved:
                transition(gcode_state)

            # Update telemetry + heartbeat (throttled to every 10 seconds)
            if self._transport_offline or time.time() - getattr(self, '_last_heartbeat', 0) >= 10:
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
                            try:
                                toggled_at = cooldown_row[0]
                                if isinstance(toggled_at, str):
                                    toggled_at = datetime.fromisoformat(toggled_at)
                                if not isinstance(toggled_at, datetime):
                                    raise ValueError('Invalid light toggle timestamp')
                                if toggled_at.tzinfo is None:
                                    toggled_at = toggled_at.replace(tzinfo=timezone.utc)
                                age = (datetime.now(timezone.utc) - toggled_at).total_seconds()
                                if 0 <= age < 20:
                                    lights_on = None
                            except (ValueError, TypeError, OverflowError):
                                log.debug("Invalid light toggle timestamp; reconciling observed state")
                        noz_type = self._state.get('nozzle_type')
                        # Partial packets are not configuration removal, and a
                        # cached diameter must not overwrite a later edit.
                        noz_dia = raw.get('nozzle_diameter')
                        if isinstance(noz_dia, bool) or not isinstance(noz_dia, (int, float, str)):
                            noz_dia = None
                        else:
                            try:
                                noz_dia = float(noz_dia)
                            except (TypeError, ValueError, OverflowError):
                                noz_dia = None
                            if noz_dia is not None:
                                import math
                                if not math.isfinite(noz_dia) or noz_dia <= 0:
                                    noz_dia = None
                        fan_speed_val = self._state.get('cooling_fan_speed')
                        conn.execute(
                            f"UPDATE printers SET last_seen={sql.now()},"
                            " bed_temp=?,bed_target_temp=?,nozzle_temp=?,nozzle_target_temp=?,"
                            " gcode_state=?,print_stage=?,hms_errors=?,"
                            " nozzle_type=?,fan_speed=COALESCE(?,fan_speed) WHERE id=?",
                            (bed_t, bed_tt, noz_t, noz_tt, gstate, stage,
                             hms_j, noz_type, fan_speed_val, self.printer_id))
                        if noz_dia is not None:
                            conn.execute("UPDATE printers SET nozzle_diameter=? WHERE id=?",
                                         (noz_dia, self.printer_id))
                        from modules.printers.monitors.light_state import persist_light_observation
                        persist_light_observation(conn, self.printer_id, lights_on, cooldown_row[0] if cooldown_row else None)
                        for column, (value, stamp, observed_at) in pending_nozzles.items():
                            # Column names come only from the fixed observation map above.
                            conn.execute(f'UPDATE printers SET {column}=?,{stamp}=? WHERE id=?',
                                         (value, observed_at.isoformat(), self.printer_id))
                        conn.commit()
                        self._pending_right_nozzle = {}

                        # Auto-detect printer model from MQTT — write once, never overwrite a user-set value.
                        # print.printer_type when present, else the ota product_name
                        # from the get_version reply (what real printers send).
                        raw_pt = (self._state.get('printer_type', '') or getattr(status, 'printer_type', '')
                                  or getattr(self, '_reported_model', '') or '')
                        if not isinstance(raw_pt, str):
                            raw_pt = ''
                        from modules.printers.printer_models import bambu_model_name
                        if raw_pt:
                            try:
                                friendly = bambu_model_name(raw_pt) or raw_pt
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
                                detected_type = bambu_model_name(raw_pt) or raw_pt
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
                            except Exception as e:
                                log.debug(f"Failed to push printer state event: {e}")
                        self._last_heartbeat = time.time()
                        self._transport_offline = False

                        # ---- Timeseries Telemetry Capture ----
                        if gstate in ('RUNNING', 'PREPARE', 'PAUSE') and time.time() - getattr(self, '_last_telemetry_insert', 0) >= 60:
                            telemetry_capture_at = time.time()
                            try:
                                conn.execute(
                                    "INSERT INTO printer_telemetry (printer_id, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed) VALUES (?, ?, ?, ?, ?, ?)",
                                    (self.printer_id, bed_t, noz_t, bed_tt, noz_tt, fan_speed_val)
                                )
                                conn.execute(f"DELETE FROM printer_telemetry WHERE recorded_at < {sql.now_offset('-90 days')}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — sql.* helpers return constant SQL fragments, no user input
                                conn.commit()
                                self._last_telemetry_insert = telemetry_capture_at
                            except Exception as e:
                                conn.rollback()
                                log.debug(f"[{self.name}] Telemetry insert: {e}")

                        # ---- AMS Environmental Data Capture ----
                        if time.time() - getattr(self, '_last_ams_env', 0) >= 300:
                            ams_capture_at = time.time()
                            try:
                                ams_raw_env = self._state.get('ams', {})
                                for entry in parse_ams_env(ams_raw_env):
                                    conn.execute(
                                        "INSERT INTO ams_telemetry (printer_id, ams_unit, humidity, temperature) VALUES (?, ?, ?, ?)",
                                        (self.printer_id, entry['unit_idx'], entry['humidity'], entry['temperature'])
                                    )
                                conn.execute(  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — sql.* helpers return constant SQL fragments, no user input
                                    f"DELETE FROM ams_telemetry WHERE recorded_at < {sql.now_offset('-90 days')}"
                                )
                                conn.commit()
                                self._last_ams_env = ams_capture_at
                            except Exception as e:
                                conn.rollback()
                                log.debug(f"[{self.name}] AMS env capture: {e}")

                        # ---- AMS Filament Slot Auto-Sync ----
                        pending_ams = getattr(self, '_pending_ams_report', None)
                        now = time.time()
                        if (pending_ams is not None
                                and now - getattr(self, '_last_ams_slot_sync', 0) >= 60
                                and now >= getattr(self, '_ams_sync_retry_at', 0.0)):
                            # Inventory receives only the latest reported AMS list; it is
                            # consumed after a committed sync. A heartbeat cannot renew
                            # cached tray observations. A failed sync retries with
                            # exponential backoff and is dropped after
                            # AMS_SYNC_MAX_ATTEMPTS (or replaced by a newer report).
                            try:
                                from modules.printers.monitors.ams_inventory import sync_ams_inventory
                                sync_ams_inventory(conn, self.printer_id, pending_ams)
                                conn.commit()
                                self._last_ams_slot_sync = now
                                if self._pending_ams_report is pending_ams:
                                    self._reset_pending_ams()
                            except Exception:
                                conn.rollback()
                                failures = getattr(self, '_ams_sync_failures', 0) + 1
                                if failures >= AMS_SYNC_MAX_ATTEMPTS:
                                    log.error("AMS inventory synchronization failed %s times; dropping the reported AMS list until the printer reports again",
                                              failures, exc_info=True)
                                    self._reset_pending_ams()
                                else:
                                    delay = min(AMS_SYNC_RETRY_BASE_SECONDS * 2 ** (failures - 1), AMS_SYNC_RETRY_MAX_SECONDS)
                                    self._ams_sync_failures = failures
                                    self._ams_sync_retry_at = now + delay
                                    log.warning("AMS inventory synchronization failed (attempt %s/%s); retrying in %ss",
                                                failures, AMS_SYNC_MAX_ATTEMPTS, delay)

                    # ---- Dual-Nozzle (H2D / H2D Pro / H2C) / External Spool Parsing ----
                    h2d_nozzle_data = None
                    external_spools = None
                    try:
                        with get_db() as h2d_conn:
                            mt_row = h2d_conn.execute('SELECT machine_type FROM printers WHERE id=?', (self.printer_id,)).fetchone()
                            machine_type = mt_row[0] if mt_row else None
                    except Exception:
                        machine_type = None

                    from modules.printers.printer_models import bambu_nozzle_count
                    if bambu_nozzle_count(machine_type) == 2:
                        h2d_nozzle_data = parse_h2d_nozzles(self._state)
                        external_spools = parse_h2d_external_spools(self._state)

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
                        **parse_bambu_speed(self._state),
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
                            with get_db() as hconn:
                                try:
                                    parsed = printer_events.parse_hms_errors(hms_raw)
                                    for err in parsed:
                                        hconn.execute(
                                            "INSERT INTO hms_error_history (printer_id, code, message, severity, source) VALUES (?, ?, ?, ?, ?)",
                                            (self.printer_id, err.get('code', ''), err.get('message', ''), err.get('severity', 'warning'), 'bambu_hms')
                                        )
                                    hconn.execute(f"DELETE FROM hms_error_history WHERE occurred_at < {sql.now_offset('-90 days')}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — sql.* helpers return constant SQL fragments, no user input
                                    hconn.commit()
                                    self._last_hms_key = hms_key
                                except Exception as e:
                                    hconn.rollback()
                                    log.debug(f"[{self.name}] HMS history insert: {e}")
                except Exception as e:
                    log.warning(f"Failed to update telemetry for printer {self.printer_id}: {e}")

            # Check for camera URL auto-discovery (only X1C/H2D broadcast rtsp_url)
            # Only sync once per connection — MQTT broadcasts this on every message
            # and each sync restarts go2rtc, killing all active camera streams.
            if not self._camera_discovered:
                ipcam = raw.get('ipcam', {})
                rtsp_url = ipcam.get('rtsp_url')
                if rtsp_url:
                    full_url = f"rtsps://bblp:{urlquote(self.access_code, safe='')}@{self.ip}:322/streaming/live/1"
                    printer_events.discover_camera(self.printer_id, full_url)
                    self._camera_discovered = True

            # Keep AMS remain percentages fresh for filament tracking
            ams_raw = self._state.get('ams', {})
            ams_units = ams_raw.get('ams', [])
            if ams_units:
                ams_remain = {}
                from modules.printers.telemetry.bambu.ams_identity import ams_trays_by_slot
                from modules.printers.monitors.ams_inventory import remaining_percent
                for slot_idx, tray in ams_trays_by_slot(ams_units):
                    known_remaining = remaining_percent(tray.get('remain'))
                    if known_remaining is not None:
                        ams_remain[slot_idx] = known_remaining
                self._state['_ams_remain'] = ams_remain

            # Update progress if we have an active job (throttled to every 5 seconds)
            if self._current_job_id and time.time() - self._last_progress_update >= 5:
                self._update_progress()

            # Check AMS spool levels (throttled to every 60 seconds)
            if time.time() - self._last_spool_check >= 60:
                self._check_spool_levels()
                self._last_spool_check = time.time()

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
        # Guard: already tracking a job — skip duplicate start
        if self._current_job_id:
            log.debug(f"[{self.name}] _job_started called but already tracking job {self._current_job_id}")
            return True

        # Check for existing running job in DB (e.g. after reconnect)
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT id, job_name, scheduled_job_id, filename FROM print_jobs "
                    "WHERE printer_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
                    (self.printer_id,)).fetchone()
                if row:
                    from core.registry import registry

                    policy = registry.get_provider("EducationPolicyProvider")
                    incoming_name = self._state.get('subtask_name') or self._state.get('gcode_file')
                    classification = (
                        policy.classify_monitor_observation(
                            conn,
                            printer_id=self.printer_id,
                            observed_filename=incoming_name,
                        )
                        if policy
                        else None
                    )
                    if classification and (
                        classification.get("education_owned")
                        or classification.get("education_reserved")
                    ):
                        claimed = policy.claim_monitor_observation(
                            conn,
                            print_job_id=row[0],
                            printer_id=self.printer_id,
                            observed_filename=incoming_name,
                        )
                        if not claimed.get("authorized"):
                            row = None
                        else:
                            conn.commit()
                            self._linked_job_id = claimed.get("job_id")
                    else:
                        from core.dispatch_observations import generic_observation_reserved
                        if generic_observation_reserved(conn,printer_id=self.printer_id,observed_filename=self._state.get('subtask_name')):
                            # Exact persisted identity/resume is owned by the transactional lifecycle helper.
                            row=None
                    if not row:
                        log.info(
                            "[%s] Existing running observation requires authoritative correlation",
                            self.name,
                        )
                    else:
                        self._current_job_id = row[0]
                        if self._linked_job_id is None:
                            self._linked_job_id = row[2]
                    # Restore spool weight snapshot (best-effort)
                        self._start_spool_weights = {}
                        try:
                            spool_rows = conn.execute("""
                                SELECT s.id, s.remaining_weight_g
                                FROM filament_slots fs
                                JOIN spools s ON fs.assigned_spool_id = s.id
                                WHERE fs.printer_id = ? AND fs.assigned_spool_id IS NOT NULL
                            """, (self.printer_id,)).fetchall()
                            for spool_id, weight in spool_rows:
                                if weight is not None:
                                    self._start_spool_weights[spool_id] = weight
                        except Exception as e:
                            log.debug(f"Failed to load spool weights for job resume: {e}")
                        log.info(f"[{self.name}] Resumed tracking existing job {row[0]} ({row[1]})")
                        return True
        except Exception as e:
            log.warning(f"[{self.name}] DB check for running job failed: {e}")
            return False

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
        return new_job_id is not None

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
        except Exception:
            log.error("Printer progress persistence failed; will retry")

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
