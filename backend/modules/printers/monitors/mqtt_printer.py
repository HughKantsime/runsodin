"""
PrinterMonitor — single-printer MQTT connection manager.

Handles connect/disconnect, MQTT state merging, telemetry DB writes,
progress tracking, spool level checks, and state-transition dispatch.
Job lifecycle and telemetry parsing helpers are in mqtt_job_lifecycle.py
and mqtt_telemetry.py respectively.
"""

from core.base import AlertType, AlertSeverity

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
# Resolved at call time: never a stale get_db bound at import.
from core.db_live import live_get_db as get_db
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


def _ams_report_digest(report) -> str:
    """Content hash of a reported AMS list (key order independent)."""
    import hashlib
    return hashlib.sha256(json.dumps(report, sort_keys=True, default=str).encode()).hexdigest()


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
                            f"UPDATE print_jobs SET status = ?, ended_at = {sql.now()} WHERE id = ?",
                            ("completed",row[0]))
                        log.info(f"[{self.name}] Closed orphaned job {row[0]} ({row[1]}) — printer is {gcode_state or 'idle'}")
                from core.job_terminal_observations import claim_terminal, terminal_event
                recovered_events=[]
                for row in rows:
                    claim_terminal(conn,row[0])
                    item=terminal_event(conn,row[0])
                    if item is not None:recovered_events.append(item)
                conn.commit()
                from core.job_timeline import enqueue
                for item in recovered_events:enqueue(item)
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

    def _ams_sync_warning_due(self, reason, now):
        # Preserve per-reason throttles across reports/reconnects. Expire old
        # entries so changing association IDs cannot grow this state forever.
        recent = {key: stamp for key, stamp in getattr(self, '_ams_warning_times', {}).items()
                  if now - stamp < 60}
        self._ams_warning_times = recent
        if reason in recent:
            return False
        recent[reason] = now
        return True

    def _reset_pending_ams(self, report=None):
        """Clear the held AMS list and all retry state (new connection or committed sync)."""
        self._pending_ams_report = report
        self._pending_ams_digest = None if report is None else _ams_report_digest(report)
        self._ams_dropped_digest = None
        self._ams_sync_failures = 0
        self._ams_sync_retry_at = 0.0

    def _note_ams_report(self, report):
        """Hold a reported AMS list for the next heartbeat sync.

        Identical contents (by hash) keep the current backoff state, so a
        printer re-sending the same list cannot reset the attempt counter; a
        list dropped after AMS_SYNC_MAX_ATTEMPTS is not retried until its
        contents change or the connection is replaced.
        """
        digest = _ams_report_digest(report)
        if digest == getattr(self, '_ams_dropped_digest', None):
            return
        if getattr(self, '_pending_ams_report', None) is not None and digest == getattr(self, '_pending_ams_digest', None):
            self._pending_ams_report = report
            return
        self._pending_ams_report = report
        self._pending_ams_digest = digest
        self._ams_dropped_digest = None
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

    def _dispatch_alert(self, alert_type: AlertType, severity: AlertSeverity, title: str,
                        message: str = "", job_id: int = None, spool_id: int = None,
                        metadata: dict = None):
        """Use the shared scoped alert policy in monitor processes too."""
        from modules.notifications.alert_dispatch import dispatch_alert
        return dispatch_alert(alert_type, severity, title, message,
            printer_id=self.printer_id, job_id=job_id, spool_id=spool_id, metadata=metadata)

    def _on_status(self, status, generation=None):
        from modules.printers.monitors.mqtt_status import handle_status
        return handle_status(self, status, generation, sys.modules[__name__])

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
                            alert_type=AlertType.SPOOL_LOW,
                            severity=AlertSeverity.WARNING,
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

        from core.job_timeline import for_printer
        # Entering RUNNING or PAUSE without a tracked job = new job
        if new_state in ('RUNNING', 'PAUSE') and not self._current_job_id:
            if old_state in (None, 'IDLE', 'FINISH', 'FAILED', 'PREPARE', 'PAUSE'):
                self._job_started()

        # RUNNING/PAUSE -> FINISH = Job completed
        elif new_state == 'FINISH' and old_state in ('RUNNING', 'PAUSE'):
            self._job_ended('completed')

        # RUNNING/PAUSE -> FAILED = Job failed
        elif new_state == 'FAILED' and old_state in ('RUNNING', 'PAUSE'):
            # Bambu also reports an explicit user cancellation as FAILED.
            error = self._state.get('print_error')
            cancelled = error in (0x0300400C, 0x0500400E, str(0x0300400C), str(0x0500400E))
            self._job_ended('cancelled' if cancelled else 'failed')

        # RUNNING/PAUSE -> IDLE = Job cancelled
        elif new_state == 'IDLE' and old_state in ('RUNNING', 'PAUSE'):
            self._job_ended('cancelled')

        # FINISH/FAILED -> IDLE = printer fully settled, attempt next queued job
        elif new_state == 'IDLE' and old_state in ('FINISH', 'FAILED'):
            Thread(target=self._try_dispatch, daemon=True).start()

        if new_state == 'PAUSE' and old_state != 'PAUSE':
            from modules.notifications.event_dispatcher import parse_hms_errors
            errors = parse_hms_errors(self._state.get('hms') or [])
            worst = max(errors, key=lambda e: {'critical': 3, 'error': 2, 'warning': 1}.get(e['severity'], 0)) if errors else None
            code = self._state.get('print_error') or (worst['code'] if worst else None)
            for_printer(self.printer_id, 'paused', code=str(code).upper() if code else None,
                        context={'before_printing': not self._state.get('layer_num', 0),
                                 'hms_codes': [error['code'] for error in errors],
                                 'print_error': self._state.get('print_error'),
                                 'filename': self._state.get('gcode_file') or self._state.get('subtask_name')})
            if self._state.get('stg_cur') == 6:
                for_printer(self.printer_id, 'runout')
        elif new_state != 'PAUSE':
            if old_state == 'PAUSE' and new_state == 'RUNNING':
                for_printer(self.printer_id, 'resumed')
            else:
                from core.job_timeline import close_pause_episode
                close_pause_episode(self.printer_id)

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
            tray_evidence_incomplete=(getattr(self, '_tray_attempt_id', None) == self._current_job_id
                                      and bool(getattr(self, '_pending_attempt_trays', ()))),
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
