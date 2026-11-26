"""MQTT status ingestion; dependencies resolved from the owning monitor at call time."""

from time import monotonic

from core.hms_state import is_notice
from core.monitor_observations import fresh_report
from core.printer_identity_audit import observe_printer_identity

RETAINED_REPORT_LOG_SECONDS = 3600
AMS_ENV_INTERVAL = 300
# Reconnecting can replace the monitor object for the same printer.
_attention_warning_after = {}


def log_retained_report(self, raw, runtime):
    """Whether Bambu brokers ever replay reports is unknown; this line is how a deploy can tell."""
    now = runtime.time.time()
    if now - getattr(self, '_last_retained_report_log', 0) >= RETAINED_REPORT_LOG_SECONDS:
        self._last_retained_report_log = now
        runtime.log.warning('Retained MQTT report: printer_id=%s gcode_state=%r', self.printer_id, raw.get('gcode_state'))


def refresh_ams_remain(self):
    """Keep AMS remain percentages fresh for filament tracking."""
    # This runs before a terminal transition: a malformed report must not cost the print its end.
    ams_raw = self._state.get('ams', {})
    ams_units = ams_raw.get('ams', []) if isinstance(ams_raw, dict) else None
    if ams_units and isinstance(ams_units, list):
        ams_remain = {}
        from modules.printers.telemetry.bambu.ams_identity import ams_trays_by_slot, is_rfid_tag
        from modules.printers.monitors.ams_inventory import remaining_percent
        for slot_idx, tray in ams_trays_by_slot(ams_units, h3c=getattr(self, '_identified_h3c', False)):
            known_remaining = remaining_percent(tray.get('remain'))
            if known_remaining is not None and (is_rfid_tag(tray.get('tag_uid')) or is_rfid_tag(tray.get('tray_uuid'))):
                ams_remain[slot_idx] = known_remaining
        self._state['_ams_remain'] = ams_remain


def handle_status(self, status, generation, runtime):
    """Handle incoming MQTT status update."""
    import modules.notifications.event_dispatcher as printer_events
    from modules.printers.hms_codes import printer_reports_no_error
    from core.printer_display_facts import AttentionUpdateFailed, persist_attention
    from modules.printers.dispatch_readiness import state_needs_attention
    with self._lock:
        if self._connection_retired or (generation is not None and generation != self._connection_generation):
            return
        raw = status.raw_data.get('print', {})
        if not isinstance(raw, dict):
            return
        # Keep the live delta seed across a refused status commit, but publish
        # no evidence until status persistence below succeeds (or is throttled).
        from modules.printers.monitors.live_state import stage_mqtt
        stage_mqtt(self, status, raw)
        from modules.printers.monitors.terminal_evidence import capture
        from modules.printers.monitors.pause_cause import observe as observe_pause, stage as pause_stage
        capture(self, raw, runtime.log)
        observe_pause(self, raw)
        if getattr(status, 'mqtt_retained', None) is True:
            log_retained_report(self, raw, runtime)
        from modules.printers.monitors.nozzle_observations import observe
        observe(self, raw, status, runtime)
        from core.printer_display_facts import observe as observe_display_facts
        observe_display_facts(self, raw, retained=getattr(status, 'mqtt_retained', None) is True)
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
        if noz_dia is not None:
            self._pending_nozzle_diameter = noz_dia
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
        h3c = getattr(self, '_identified_h3c', False)
        if isinstance(incoming_ams_report, dict) and 'ams' in incoming_ams_report:
            # get_version may identify the rack after a partial zone update.
            from modules.printers.monitors.mqtt_telemetry import parse_ams_env
            zones = getattr(self, '_ams_zones', {})
            for sample in parse_ams_env(incoming_ams_report, h3c=True):
                zones[sample['unit_idx']] = (runtime.time.time(), sample)
            self._ams_zones = zones

        cached = getattr(self, '_inventory_ams_units', [])
        if isinstance(incoming_ams_report, dict) and (
                'ams' in incoming_ams_report or ('tray_exist_bits' in incoming_ams_report and (not h3c or cached))):
            # Held until the next heartbeat write: Bambu sends the AMS list
            # only on change/pushall, usually between heartbeats.
            from modules.printers.telemetry.bambu.ams_identity import inventory_report, merge_rack_trays
            from copy import deepcopy
            pending = getattr(self, '_pending_ams_report', None)
            units = incoming_ams_report.get('ams', cached)
            report = inventory_report(units, incoming_ams_report.get('tray_exist_bits'),
                                      cached if h3c else pending or cached,
                                      h3c=h3c, bit_only='ams' not in incoming_ams_report)
            # Keep rack fields for a later get_version even before they can update inventory.
            self._inventory_ams_units = deepcopy(merge_rack_trays(cached, report))
            if h3c:
                report = merge_rack_trays(pending, report)
            from modules.printers.telemetry.bambu.ams_identity import ams_trays_by_slot
            for _, tray in ams_trays_by_slot(self._inventory_ams_units, h3c=h3c):
                tray.pop('_removed', None)
            self._note_ams_report(report)
            # Bind repair evidence to this exact inventory batch, including across a throttled heartbeat.
            from modules.printers.bambu_slots import full_ams_slots
            self._phantom_slot_evidence = None
            received_at = getattr(status, 'mqtt_received_at', None)
            if (not h3c and getattr(status, 'mqtt_retained', None) is False and received_at is not None
                    and fresh_report(False, runtime.time.time() - received_at)):
                numbers = full_ams_slots(raw)
                if numbers:
                    self._phantom_slot_evidence = (report, numbers, received_at)

        elif isinstance(incoming_ams_report, dict) and 'ams_exist_bits' in incoming_ams_report:
            self._phantom_slot_evidence = None  # A newer mask without the matching list.

        # Merge correlation and lifecycle fields before any HMS processing.
        # A first RUNNING packet may also contain a print-stopping HMS code;
        # the token observation must be claimed/quarantined first.
        speed_fields = runtime.parse_bambu_speed(raw)
        for key, value in raw.items():
            if key == 's_obj':
                continue  # bound to the current print after lifecycle transitions below
            if key in ('nozzle_temper_1', 'nozzle_target_temper_1'):
                value = runtime.finite_nozzle_measurement(value)
            elif key in ('spd_lvl', 'spd_mag'):
                value = speed_fields['speed_level' if key == 'spd_lvl' else 'speed_percent']
            if value is not None:
                self._state[key] = value
        if raw.get('gcode_state') is not None:
            self._state_retained = getattr(status, 'mqtt_retained', None) is True
        gcode_state = self._state.get('gcode_state')
        if self._state_retained and gcode_state in ('FINISH', 'FAILED', 'IDLE'):
            # Replay may update display, but cannot bypass the HMS gate via the
            # normal lifecycle path. Deltas cannot promote that cached replay.
            gcode_state = None
        if gcode_state in ('FINISH', 'FAILED', 'IDLE'):
            # An end measures filament against this packet, which after downtime is the only
            # report there is. A start waits for the tail: its snapshot is first-start evidence,
            # and a first packet can be a print already under way.
            refresh_ams_remain(self)
        # A question owns only physical bookkeeping. Telemetry continues below,
        # while start, progress and HMS terminal writers have no row authority.
        from modules.printers.monitors.restart_question import handle as handle_restart_question
        question_waiting = bool(gcode_state and not self._current_job_id and handle_restart_question(self, runtime))
        self._restart_blocks_rows = question_waiting
        if question_waiting:
            self._pending_running_state = None
        if not question_waiting and gcode_state and getattr(self, '_recovery_pending', False) and not self._current_job_id:
            # Recovery's wait ended before any report carried a state, or it failed. A tracked
            # print is ending live, so its row waits for this packet's transition instead.
            self._reconcile_running_rows()
        tracked_before = self._current_job_id
        pending_start_before = self._pending_running_state
        self._status_received.set()
        if not question_waiting and gcode_state in ('RUNNING', 'PAUSE') and not self._current_job_id:
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
                          (value in ('RUNNING', 'PAUSE') and not self._current_job_id and not question_waiting)):
                self._on_state_change(self._last_gcode_state, value)
                if not (self._pending_running_state and not self._current_job_id):
                    self._last_gcode_state = value

        terminal = gcode_state in ('FINISH', 'FAILED', 'IDLE')
        if not terminal:
            # Initial claim/quarantine must precede HMS authority resolution.
            transition(gcode_state)
        if self._current_job_id:
            self._pending_running_state = None

        # The printer's own camera reports (docs/bambu-hms.md): recorded and alerted
        # only, before a terminal packet ends the print they belong to. Never raises.
        from modules.vision.services import observe_printer_detection
        observe_printer_detection(self, raw, status)

        # Capture every fresh tray delta, including packets between heartbeats.
        # Cached AMS state can belong to a prior print, so never use it here.
        if self._current_job_id:
            from core.interfaces.attempt_accounting import attempt_accounting
            accounting = attempt_accounting()
            if getattr(self, '_tray_attempt_id', None) != self._current_job_id:
                self._tray_attempt_id = self._current_job_id
                self._recorded_attempt_trays = set()
                self._pending_attempt_trays = set()
            tray = accounting.valid_ams_tray(incoming_ams_report.get('tray_now')) if isinstance(incoming_ams_report, dict) else None
            if h3c and tray is not None and tray >= 9:
                tray = None
            if gcode_state in ('RUNNING', 'PAUSE') and tray is not None and tray not in self._recorded_attempt_trays:
                self._pending_attempt_trays.add(tray)
            if self._pending_attempt_trays:
                try:
                    with runtime.get_db() as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        for observed_tray in sorted(self._pending_attempt_trays):
                            accounting.record_attempt_tray(conn, self._current_job_id, self.printer_id, observed_tray)
                        conn.commit()
                    self._recorded_attempt_trays.update(self._pending_attempt_trays)
                    self._pending_attempt_trays.clear()
                except Exception:
                    # Keep evidence for retry without stalling HMS/lifecycle/heartbeat.
                    # Terminal writers durably mark any still-pending evidence.
                    runtime.log.warning('[%s] Attempt tray persistence failed; will retry', self.name)

        quiet_unknown = printer_reports_no_error(self._state.get('print_error'), gcode_state)

        def process_hms(packet, expected_print_job_id=None):
            from modules.printers.monitors.hms_evidence import evidence
            report_evidence = evidence(packet, runtime.time.time())
            incoming_hms = packet.get('hms')
            if isinstance(incoming_hms, list) and not incoming_hms:
                self._last_hms_processed_key = None
            if not isinstance(incoming_hms, list):
                return True
            # HMS belongs to this packet, never an identity from cached telemetry.
            observed_filename = packet.get('subtask_name') or packet.get('gcode_file')
            corroborated = (fresh_report(report_evidence['mqtt_retained'], report_evidence['report_age_seconds'])
                            and report_evidence['live_state'] in ('FAILED', 'PAUSE', 'IDLE'))
            quiet = packet.get('_quiet_unknown', quiet_unknown)
            process_key = (expected_print_job_id, observed_filename,
                           runtime.json.dumps(incoming_hms, sort_keys=True), corroborated, quiet)
            previous = getattr(self, '_last_hms_processed_key', None)
            if (process_key == previous or (previous and previous[3] and previous[4] == quiet
                    and process_key[1:3] == previous[1:3]
                    and expected_print_job_id in (None, previous[0]))):
                return True
            try:
                result = printer_events.process_hms_errors(
                    self.printer_id, incoming_hms, observed_filename=observed_filename,
                    expected_print_job_id=expected_print_job_id,
                    **report_evidence, **({'quiet_unknown': True} if quiet else {}),
                    **({'withhold_row_writes': True} if question_waiting else {}),
                    tray_evidence_incomplete=(getattr(self, '_tray_attempt_id', None) == expected_print_job_id
                                              and bool(getattr(self, '_pending_attempt_trays', ()))))
            except Exception:
                return False
            if isinstance(result, dict) and result.get('retryable') is True:
                return False
            self._last_hms_processed_key = process_key
            return True

        pending_hms = self._pending_hms_terminal
        if (pending_hms is not None and pending_hms.get('generation', self._connection_generation)
                != self._connection_generation):
            self._pending_hms_terminal = None
            pending_hms = None
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
        current_report_fresh = fresh_report(getattr(status, 'mqtt_retained', False),
                                            getattr(status, 'report_age_seconds', None))
        resolved = pending_hms is None
        if pending_hms is not None and current_report_fresh:
            # Untracked prints (no job then, none now) retry under the same
            # no-job identity; otherwise only a bound original job may resolve.
            untracked = pending_hms['print_job_id'] is None and self._current_job_id is None
            from modules.printers.monitors.hms_evidence import corroborate_retry
            pending_hms['packet'] = corroborate_retry(pending_hms['packet'], raw, status, runtime.time.time())
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
            from modules.printers.monitors.hms_evidence import prepare
            hms_packet = prepare(self, raw, status, runtime.time.time())
            previous = getattr(self, '_last_hms_processed_key', None)
            if ('hms' not in hms_packet and previous and previous[4] != quiet_unknown
                    and self._state.get('hms')):
                # Reclassify the displayed codes after a state-only delta. A cached code
                # cannot supply fresh terminal evidence for the current job.
                hms_packet = dict(hms_packet, hms=self._state['hms'], gcode_state=None)
            hms_packet['_quiet_unknown'] = quiet_unknown
            resolved = process_hms(hms_packet, self._current_job_id)
            if not resolved and hms_packet.get('hms'):
                from copy import deepcopy
                # One bounded observation, not an accumulating event history.
                self._pending_hms_terminal = {
                    'packet': deepcopy({k: hms_packet[k] for k in
                        ('hms', 'subtask_name', 'gcode_file', 'gcode_state',
                         '_hms_retained', '_hms_report_age', '_hms_received_at', '_quiet_unknown') if k in hms_packet}),
                    'state': dict(self._state), 'terminal': gcode_state if terminal else None,
                    'print_job_id': self._current_job_id,
                    'generation': self._connection_generation,
                    'start': self._pending_running_state,
                }
        if terminal and resolved:
            transition(gcode_state)

        if terminal and not question_waiting and not self._current_job_id and getattr(self, '_restart_check_needed', False):
            from modules.printers.monitors.restart_question import clear_finished
            try:
                clear_finished(self, runtime)
            except Exception:
                runtime.log.error('[%s] Print confirmation cleanup will retry', self.name)

        # Write FAILED episode changes immediately, even between regular heartbeats.
        live_state_allowed = True
        gstate = self._state.get('gcode_state')
        error = getattr(self, '_display_print_error', None) if not self._state_retained else None
        attention = (gstate == 'FAILED', error)
        heartbeat_due = (self._transport_offline or attention != getattr(self, '_heartbeat_attention', (False, None))
                         or runtime.time.time() - getattr(self, '_last_heartbeat', 0) >= 10)
        if heartbeat_due:
            live_state_allowed = False
            try:
                with runtime.get_db() as conn:
                    bed_t = self._state.get('bed_temper')
                    bed_tt = self._state.get('bed_target_temper')
                    noz_t = self._state.get('nozzle_temper')
                    noz_tt = self._state.get('nozzle_target_temper')
                    stg_cur = self._state.get('stg_cur', -1)
                    stage = pause_stage(self, runtime.resolve_stage_label(stg_cur, gstate))
                    lights_on_raw = runtime.parse_lights(self._state.get('lights_report', []))
                    # Check cooldown — don't overwrite if toggled via API within last 20s
                    printer_config = conn.execute('SELECT lights_toggled_at,model,machine_type FROM printers WHERE id=?', (self.printer_id,)).fetchone()
                    self._stored_models = tuple(printer_config[1:]) if printer_config else ()
                    lights_on = lights_on_raw
                    if printer_config and printer_config[0]:
                        try:
                            toggled_at = printer_config[0]
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
                    # Only observations since the last commit remain pending.
                    noz_dia = getattr(self, '_pending_nozzle_diameter', None)
                    fan_speed_val = self._state.get('cooling_fan_speed')
                    conn.execute(
                        f"UPDATE printers SET last_seen={runtime.sql.now()},"
                        " bed_temp=?,bed_target_temp=?,nozzle_temp=?,nozzle_target_temp=?,"
                        " gcode_state=?,print_stage=?,"
                        " nozzle_type=?,fan_speed=COALESCE(?,fan_speed) WHERE id=?",
                        (bed_t, bed_tt, noz_t, noz_tt, gstate, stage,
                         noz_type, fan_speed_val, self.printer_id))
                    if noz_dia is not None:
                        observe_printer_identity(conn, self.printer_id, "nozzle_diameter", noz_dia)
                    from modules.printers.monitors.light_state import persist_light_observation
                    persist_light_observation(conn, self.printer_id, lights_on, printer_config[0] if printer_config else None)
                    for column, (value, stamp, observed_at) in pending_nozzles.items():
                        # Column names come only from the fixed observation map above.
                        conn.execute(f'UPDATE printers SET {column}=?,{stamp}=? WHERE id=?',  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- column/stamp names come from the literal nozzle column tuples in _on_status (closed set); values are bound
                                     (value, observed_at.isoformat(), self.printer_id))
                    from modules.printers.monitors.nozzle_observations import persist
                    unconfirmed_filaments = persist(conn, self)
                    try:
                        error = persist_attention(conn, self.printer_id, gstate, error)
                    except Exception as exc:
                        # Roll back the whole status write so the old last_seen ages through stale to offline.
                        # The attention warning below is logged at most once a minute per printer.
                        conn.rollback()
                        raise AttentionUpdateFailed from exc
                    conn.commit()
                    self._heartbeat_attention = attention
                    live_state_allowed = True
                    self._pending_nozzle_diameter = None
                    self._pending_installed_nozzles = {}
                    self._pending_rack_filaments = unconfirmed_filaments
                    self._pending_right_nozzle = {}
                    # Foundry's display cache is optional: write it after the
                    # status commit so a cache failure can't roll that back.
                    try:
                        from core.printer_display_facts import persist as persist_display_facts
                        persist_display_facts(conn, self)
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        runtime.log.debug(f"[{self.name}] Display facts cache write failed: {e}")

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
                            from modules.printers.printer_models import BAMBU_BEDS
                            if detected_type in BAMBU_BEDS:
                                try:
                                    conn.execute('SAVEPOINT observed_bed')
                                    observe_bambu_bed(conn, self.printer_id, detected_type, BAMBU_BEDS[detected_type])
                                    conn.execute('RELEASE SAVEPOINT observed_bed')
                                except Exception:
                                    # A failed SAVEPOINT can itself abort a PostgreSQL transaction.
                                    conn.rollback()
                                    observe_printer_identity(conn, self.printer_id, 'machine_type', detected_type)
                                    runtime.log.warning("Could not update bed size for printer %s", self.printer_id)
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

                    from modules.printers.printer_models import rack_model_identity
                    identity = rack_model_identity(raw_pt, getattr(self, '_reported_model', None), *self._stored_models)
                    rack = identity if identity is not None else getattr(self, '_rack_rows_stored', False)

                    # ---- AMS Environmental Data Capture ----
                    if ((not identity or getattr(self, '_ams_zones', {}))
                            and runtime.time.time() - getattr(self, '_last_ams_env', 0) >= AMS_ENV_INTERVAL):
                        ams_capture_at = runtime.time.time()
                        try:
                            ams_raw_env = self._state.get('ams', {})
                            if identity:
                                entries = [sample for seen, sample in self._ams_zones.values()
                                           if ams_capture_at - seen <= 2 * AMS_ENV_INTERVAL]
                            else:
                                entries = runtime.parse_ams_env(ams_raw_env, rack=rack)
                            for entry in entries:
                                conn.execute(
                                    "INSERT INTO ams_telemetry (printer_id, ams_unit, humidity, temperature, humidity_percent) VALUES (?, ?, ?, ?, ?)",
                                    (self.printer_id, entry['unit_idx'], entry['humidity'], entry['temperature'], entry['humidity_percent'])
                                )
                                if entry.get('label'):
                                    label = {str(entry['unit_idx']): entry['label']}
                                    conn.execute("INSERT INTO system_config(key,value) SELECT 'ams.zone.' || CAST(id AS TEXT) "
                                                 "|| '.' || CAST(created_at AS TEXT) || '.' || ?, ? FROM printers WHERE id=? "
                                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                                 (str(entry['unit_idx']), runtime.json.dumps(label), self.printer_id))
                            conn.execute(  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- constant retention interval
                                f"DELETE FROM ams_telemetry WHERE recorded_at < {runtime.sql.now_offset('-90 days')}"
                            )
                            conn.commit()
                            self._last_ams_env = ams_capture_at
                        except Exception as e:
                            conn.rollback()
                            runtime.log.debug(f"[{self.name}] AMS env capture: {e}")

                    # ---- AMS Filament Slot Auto-Sync ----
                    pending_ams = getattr(self, '_pending_ams_report', None)
                    if pending_ams is not None and rack and not identity:
                        self._reset_pending_ams()
                        pending_ams = None
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
                            if identity:
                                sync_ams_inventory(conn, self.printer_id, pending_ams, h3c=True)
                            else:
                                sync_ams_inventory(conn, self.printer_id, pending_ams)
                            evidence = getattr(self, '_phantom_slot_evidence', None)
                            if not identity and evidence and evidence[0] is pending_ams and fresh_report(False, runtime.time.time() - evidence[2]):
                                from core.print_attempts import qmark_runner
                                from modules.printers.bambu_slots import repair_phantom_slots
                                repair_phantom_slots(qmark_runner(conn), self.printer_id, evidence[1])
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
                    'state_needs_attention': state_needs_attention(gstate, 'bambu', error),
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

            except AttentionUpdateFailed:
                now = monotonic()
                if now >= _attention_warning_after.get(self.printer_id, 0):
                    _attention_warning_after[self.printer_id] = now + 60
                    runtime.log.warning("[%s] attention evidence could not be updated; status write retried", self.name)
            except Exception as e:
                runtime.log.warning(f"Failed to update telemetry for printer {self.printer_id}: {e}")

        # History follows activations, including a pause or a clear between heartbeats.
        if raw.get('hms') == []:
            self._last_hms_key = {}
        elif live_state_allowed and self._state.get('hms') and (heartbeat_due or {'hms', 'gcode_state', 'print_error'}.intersection(raw)):
            from modules.printers.bambu_hms_bundle import prefix_from_serial
            try:
                prefix = prefix_from_serial(getattr(self, 'serial', ''))
                parsed = printer_events.parse_hms_errors(self._state['hms'], prefix, quiet_unknown=quiet_unknown)
                previous = getattr(self, '_last_hms_key', None) or {}
                # Remember whether each still-active code already has an actionable history row.
                active = {}
                rows = []
                for err in parsed:
                    code = err['code']
                    notice = is_notice(err)
                    active[code] = previous.get(code, False) or not notice
                    if code not in previous or active[code] != previous[code]:
                        severity = 'error' if code in previous else err['severity']
                        rows.append((self.printer_id, code, err['message'], severity,
                                     'bambu_notice' if notice else 'bambu_hms'))
                if rows:
                    with runtime.get_db() as hconn:
                        for row in rows:
                            hconn.execute("INSERT INTO hms_error_history (printer_id, code, message, severity, source) VALUES (?, ?, ?, ?, ?)", row)
                        hconn.execute(f"DELETE FROM hms_error_history WHERE occurred_at < {runtime.sql.now_offset('-90 days')}")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- constant interval
                        hconn.commit()
                self._last_hms_key = active
            except Exception as e:
                runtime.log.debug(f"[{self.name}] HMS history insert: {e}")

        # Optional evidence never precedes an attempted status commit. Throttled
        # deltas still refresh only this connection's explicit live state.
        if live_state_allowed:
            from modules.printers.monitors.live_state import observe_mqtt
            observe_mqtt(self, status, raw, runtime)

        # A reported rtsp_url signals that the printer has a camera stream.
        # Only sync once per connection — MQTT broadcasts this on every message
        # and each sync restarts go2rtc, killing all active camera streams.
        if not self._camera_discovered:
            ipcam = raw.get('ipcam', {})
            rtsp_url = ipcam.get('rtsp_url')
            if rtsp_url:
                full_url = f"rtsps://bblp:{runtime.urlquote(self.access_code, safe='')}@{self.ip}:322/streaming/live/1"
                printer_events.discover_camera(self.printer_id, full_url)
                self._camera_discovered = True

        from modules.printers.monitor_commands import observe_skip_state
        observe_skip_state(self, raw, retained=getattr(status, 'mqtt_retained', None) is True)
        refresh_ams_remain(self)

        # Update progress if we have an active job (throttled to every 5 seconds)
        if self._current_job_id and runtime.time.time() - self._last_progress_update >= 5:
            self._update_progress()

        # Check AMS spool levels (throttled to every 60 seconds)
        if runtime.time.time() - self._last_spool_check >= 60:
            self._check_spool_levels()
            self._last_spool_check = runtime.time.time()


def observe_bambu_bed(conn, printer_id, model, dims):
    import json
    old = conn.execute('SELECT bed_x_mm,bed_y_mm FROM printers WHERE id=?', (printer_id,)).fetchone()
    if old is None:
        return
    if tuple(old) == (None, None):
        before = None
    elif model in ('H2D', 'H2D Pro') and tuple(old) == (320, 320):
        before = [320, 320]
    else:
        return
    edits = conn.execute("SELECT details FROM audit_logs WHERE entity_type='printer' AND entity_id=? "
                         "AND action='printer.updated'", (printer_id,))
    from core.display_json import display_object
    for (details,) in edits:
        audit = display_object(details)
        if audit is None:
            return
        fields = audit.get('fields')
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            return
        if audit.get('fields_mean') == 'changed' and (
                'bed_x_mm' in fields or 'bed_y_mm' in fields):
            return
    # Machine-type observation already holds the printer write lock in this transaction.
    conn.execute('UPDATE printers SET bed_x_mm=?,bed_y_mm=? WHERE id=?', (*dims, printer_id))
    conn.execute("INSERT INTO audit_logs(action,entity_type,entity_id,details) "
                 "VALUES('printer.observed_config_changed','printer',?,?)",
                 (printer_id, json.dumps({'field': 'bed_size', 'old': before, 'new': dims})))
