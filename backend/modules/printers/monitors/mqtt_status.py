"""MQTT status ingestion; dependencies resolved from the owning monitor at call time."""

from core.printer_identity_audit import observe_printer_identity


def handle_status(self, status, generation, runtime):
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
            measured = runtime.finite_nozzle_measurement(raw.get(key))
            if measured is not None:
                pending_nozzles[column] = (measured, stamp, datetime.now(timezone.utc))
        # Real H2D firmware reports the second extruder only in
        # device.extruder.info[id=1] (packed current|target<<16).
        second_extruder = runtime.h2d_extruder_temperatures(raw).get(1)
        if second_extruder is not None:
            observed = datetime.now(timezone.utc)
            pending_nozzles['nozzle_1_temp'] = (second_extruder[0], 'nozzle_1_temp_observed_at', observed)
            pending_nozzles['nozzle_1_target_temp'] = (second_extruder[1], 'nozzle_1_target_observed_at', observed)
        self._pending_right_nozzle = pending_nozzles
        incoming_ams_report = raw.get('ams')
        if isinstance(incoming_ams_report, dict) and isinstance(incoming_ams_report.get('ams'), list):
            # Held until the next heartbeat write: Bambu sends the AMS list
            # only on change/pushall, usually between heartbeats.
            self._note_ams_report(incoming_ams_report['ams'])

        # Merge correlation and lifecycle fields before any HMS processing.
        # A first RUNNING packet may also contain a print-stopping HMS code;
        # the token observation must be claimed/quarantined first.
        speed_fields = runtime.parse_bambu_speed(raw)
        for key, value in raw.items():
            if key in ('nozzle_temper_1', 'nozzle_target_temper_1'):
                value = runtime.finite_nozzle_measurement(value)
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
            process_key = (observed_filename, runtime.json.dumps(incoming_hms, sort_keys=True))
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
        if self._transport_offline or runtime.time.time() - getattr(self, '_last_heartbeat', 0) >= 10:
            try:
                with runtime.get_db() as conn:
                    bed_t = self._state.get('bed_temper')
                    bed_tt = self._state.get('bed_target_temper')
                    noz_t = self._state.get('nozzle_temper')
                    noz_tt = self._state.get('nozzle_target_temper')
                    gstate = self._state.get('gcode_state')
                    stg_cur = self._state.get('stg_cur', -1)
                    stage = runtime.resolve_stage_label(stg_cur, gstate)
                    import json as _json
                    hms_raw = self._state.get('hms', [])
                    hms_j = _json.dumps(hms_raw) if hms_raw else None
                    lights_on_raw = runtime.parse_lights(self._state.get('lights_report', []))
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
                            runtime.log.debug("Invalid light toggle timestamp; reconciling observed state")
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
                        f"UPDATE printers SET last_seen={runtime.sql.now()},"
                        " bed_temp=?,bed_target_temp=?,nozzle_temp=?,nozzle_target_temp=?,"
                        " gcode_state=?,print_stage=?,hms_errors=?,"
                        " nozzle_type=?,fan_speed=COALESCE(?,fan_speed) WHERE id=?",
                        (bed_t, bed_tt, noz_t, noz_tt, gstate, stage,
                         hms_j, noz_type, fan_speed_val, self.printer_id))
                    if noz_dia is not None:
                        observe_printer_identity(conn, self.printer_id, "nozzle_diameter", noz_dia)
                    from modules.printers.monitors.light_state import persist_light_observation
                    persist_light_observation(conn, self.printer_id, lights_on, cooldown_row[0] if cooldown_row else None)
                    for column, (value, stamp, observed_at) in pending_nozzles.items():
                        # Column names come only from the fixed observation map above.
                        conn.execute(f'UPDATE printers SET {column}=?,{stamp}=? WHERE id=?',  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- column/stamp names come from the literal nozzle column tuples in _on_status (closed set); values are bound
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
                            observe_printer_identity(conn, self.printer_id, 'model', friendly, only_unset=True)
                            conn.commit()
                        except Exception as e:
                            runtime.log.debug(f"[{self.name}] Model auto-detect failed: {e}")

                    # Auto-detect machine_type (H2D, X1C, P1S, etc.) — always update
                    if raw_pt:
                        try:
                            detected_type = bambu_model_name(raw_pt) or raw_pt
                            observe_printer_identity(conn, self.printer_id, 'machine_type', detected_type)
                            conn.commit()
                        except Exception as e:
                            runtime.log.debug(f"[{self.name}] machine_type detect failed: {e}")

                    # Republish telemetry to external broker
                    if runtime.mqtt_republish:
                        try:
                            runtime.mqtt_republish.republish_telemetry(self.printer_id, self.name, {
                                "bed_temp": bed_t, "bed_target": bed_tt,
                                "nozzle_temp": noz_t, "nozzle_target": noz_tt,
                                "state": gstate,
                                "progress": self._state.get('mc_percent'),
                                "remaining_min": self._state.get('mc_remaining_time'),
                                "current_layer": self._state.get('layer_num'),
                                "total_layers": self._state.get('total_layer_num'),
                            })
                        except Exception as e:
                            runtime.log.debug(f"Failed to push printer state event: {e}")
                    self._last_heartbeat = runtime.time.time()
                    self._transport_offline = False

                    # ---- Timeseries Telemetry Capture ----
                    if gstate in ('RUNNING', 'PREPARE', 'PAUSE') and runtime.time.time() - getattr(self, '_last_telemetry_insert', 0) >= 60:
                        telemetry_capture_at = runtime.time.time()
                        try:
                            conn.execute(
                                "INSERT INTO printer_telemetry (printer_id, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed) VALUES (?, ?, ?, ?, ?, ?)",
                                (self.printer_id, bed_t, noz_t, bed_tt, noz_tt, fan_speed_val)
                            )
                            conn.execute(f"DELETE FROM printer_telemetry WHERE recorded_at < {runtime.sql.now_offset('-90 days')}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — sql.* helpers return constant SQL fragments, no user input
                            conn.commit()
                            self._last_telemetry_insert = telemetry_capture_at
                        except Exception as e:
                            conn.rollback()
                            runtime.log.debug(f"[{self.name}] Telemetry insert: {e}")

                    # ---- AMS Environmental Data Capture ----
                    if runtime.time.time() - getattr(self, '_last_ams_env', 0) >= 300:
                        ams_capture_at = runtime.time.time()
                        try:
                            ams_raw_env = self._state.get('ams', {})
                            for entry in runtime.parse_ams_env(ams_raw_env):
                                conn.execute(
                                    "INSERT INTO ams_telemetry (printer_id, ams_unit, humidity, temperature) VALUES (?, ?, ?, ?)",
                                    (self.printer_id, entry['unit_idx'], entry['humidity'], entry['temperature'])
                                )
                            conn.execute(  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — sql.* helpers return constant SQL fragments, no user input
                                f"DELETE FROM ams_telemetry WHERE recorded_at < {runtime.sql.now_offset('-90 days')}"
                            )
                            conn.commit()
                            self._last_ams_env = ams_capture_at
                        except Exception as e:
                            conn.rollback()
                            runtime.log.debug(f"[{self.name}] AMS env capture: {e}")

                    # ---- AMS Filament Slot Auto-Sync ----
                    pending_ams = getattr(self, '_pending_ams_report', None)
                    now = runtime.time.time()
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
                        except Exception as exc:
                            from modules.printers.monitors.ams_inventory import AMSInventoryError
                            reason = str(exc) if type(exc) is AMSInventoryError else type(exc).__name__
                            conn.rollback()
                            failures = getattr(self, '_ams_sync_failures', 0) + 1
                            if self._pending_ams_report is not pending_ams:
                                pass  # a newer report replaced it; its own retry state applies
                            elif failures >= runtime.AMS_SYNC_MAX_ATTEMPTS:
                                runtime.log.error("AMS inventory synchronization failed %s times; dropping the reported AMS list until its contents change; printer_id=%s reason=%s",
                                          failures, self.printer_id, reason, exc_info=type(exc) is not AMSInventoryError)
                                dropped = self._pending_ams_digest
                                self._reset_pending_ams()
                                self._ams_dropped_digest = dropped
                            else:
                                delay = min(runtime.AMS_SYNC_RETRY_BASE_SECONDS * 2 ** (failures - 1), runtime.AMS_SYNC_RETRY_MAX_SECONDS)
                                self._ams_sync_failures = failures
                                self._ams_sync_retry_at = now + delay
                                if self._ams_sync_warning_due(reason, now):
                                    runtime.log.warning("AMS inventory synchronization failed (attempt %s/%s); retrying in %ss; printer_id=%s reason=%s",
                                                failures, runtime.AMS_SYNC_MAX_ATTEMPTS, delay, self.printer_id, reason,
                                                exc_info=type(exc) is not AMSInventoryError)

                # ---- Dual-Nozzle (H2D / H2D Pro / H2C) / External Spool Parsing ----
                h2d_nozzle_data = None
                external_spools = None
                try:
                    with runtime.get_db() as h2d_conn:
                        mt_row = h2d_conn.execute('SELECT machine_type FROM printers WHERE id=?', (self.printer_id,)).fetchone()
                        machine_type = mt_row[0] if mt_row else None
                except Exception:
                    machine_type = None

                from modules.printers.printer_models import bambu_nozzle_count
                if bambu_nozzle_count(machine_type) == 2:
                    h2d_nozzle_data = runtime.parse_h2d_nozzles(self._state)
                    external_spools = runtime.parse_h2d_external_spools(self._state)

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
                    **runtime.parse_bambu_speed(self._state),
                }
                if h2d_nozzle_data:
                    ws_payload['h2d_nozzles'] = h2d_nozzle_data
                if external_spools:
                    ws_payload['external_spools'] = external_spools
                runtime.ws_push('printer_telemetry', ws_payload)

                # Process HMS errors through universal handler for alerts
                if hms_raw:
                    import json as _json2
                    hms_key = _json2.dumps(hms_raw)
                    if hms_key != getattr(self, '_last_hms_key', None):
                        with runtime.get_db() as hconn:
                            try:
                                parsed = printer_events.parse_hms_errors(hms_raw)
                                for err in parsed:
                                    hconn.execute(
                                        "INSERT INTO hms_error_history (printer_id, code, message, severity, source) VALUES (?, ?, ?, ?, ?)",
                                        (self.printer_id, err.get('code', ''), err.get('message', ''), err.get('severity', 'warning'), 'bambu_hms')
                                    )
                                hconn.execute(f"DELETE FROM hms_error_history WHERE occurred_at < {runtime.sql.now_offset('-90 days')}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — sql.* helpers return constant SQL fragments, no user input
                                hconn.commit()
                                self._last_hms_key = hms_key
                            except Exception as e:
                                hconn.rollback()
                                runtime.log.debug(f"[{self.name}] HMS history insert: {e}")
            except Exception as e:
                runtime.log.warning(f"Failed to update telemetry for printer {self.printer_id}: {e}")

        # Check for camera URL auto-discovery (only X1C/H2D broadcast rtsp_url)
        # Only sync once per connection — MQTT broadcasts this on every message
        # and each sync restarts go2rtc, killing all active camera streams.
        if not self._camera_discovered:
            ipcam = raw.get('ipcam', {})
            rtsp_url = ipcam.get('rtsp_url')
            if rtsp_url:
                full_url = f"rtsps://bblp:{runtime.urlquote(self.access_code, safe='')}@{self.ip}:322/streaming/live/1"
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
        if self._current_job_id and runtime.time.time() - self._last_progress_update >= 5:
            self._update_progress()

        # Check AMS spool levels (throttled to every 60 seconds)
        if runtime.time.time() - self._last_spool_check >= 60:
            self._check_spool_levels()
            self._last_spool_check = runtime.time.time()

