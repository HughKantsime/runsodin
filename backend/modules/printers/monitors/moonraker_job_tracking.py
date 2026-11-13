"""Moonraker job tracking, split from the live monitor to keep both modules bounded."""
from core.order_production import retry_production_linkage


class MoonrakerJobTracking:
    # ==================== Job Tracking ====================

    @retry_production_linkage
    def _job_started(self, status):
        """Record a new print job starting."""
        from modules.printers.monitors.moonraker_monitor import (ProductionLinkageChanged, datetime, engine, log, time, timezone)
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
                        'nozzle_temp_target':nozzle_target},source='moonraker-first-start',allow_physical_resume=True,observed_elapsed=getattr(status,'total_duration',None))
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
        self, end_status: str, status, *, education_terminal_status: str | None = None, history=None
    ) -> bool:
        """Record a print job ending."""
        from modules.printers.monitors.moonraker_monitor import (AlertSeverity, AlertType, ProductionLinkageChanged, as_utc_datetime, datetime, engine, log, prepare_print_job_write, reconcile_job_write, text, timedelta, timezone)
        import modules.notifications.event_dispatcher as printer_events
        if not self._current_job_db_id:
            log.warning(f"[{self.name}] Job ended but no current job tracked")
            return False

        if history:
            end_status = education_terminal_status = history['status']
        ended_observation_id=self._current_job_db_id
        committed_completion = False
        completion_job_id = None
        try:
            with engine.begin() as conn:
                now_utc = history['ended_at'] if history else datetime.now(timezone.utc).isoformat()

                # Calculate duration
                duration_seconds = None
                pj_row = conn.execute(
                    text("SELECT started_at FROM print_jobs WHERE id = :jid"),
                    {"jid": self._current_job_db_id}).fetchone()
                if pj_row and pj_row[0]:
                    try:
                        started = as_utc_datetime(pj_row[0])
                        ended = as_utc_datetime(now_utc)
                        if not history and ended_observation_id == getattr(self, '_unwatched_row', None) and getattr(status, 'total_duration', None) is not None:
                            # It ran for total_duration, not until this poll first saw the end.
                            ended = min(ended, started + timedelta(seconds=float(status.total_duration)))
                            now_utc = ended.isoformat()
                        duration_seconds = int((ended - started).total_seconds())
                    except Exception as e:
                        log.debug(f"Failed to parse duration: {e}")

                if history:
                    duration_seconds = history['duration']
                    # S1 history starts a new segment at resume; retain the physical print's start.
                    if (not getattr(getattr(self, 'printer', None), 'is_kobra_s1', False)
                            and pj_row and pj_row[0] and as_utc_datetime(pj_row[0]) < as_utc_datetime(history['started_at'])):
                        conn.execute(text('UPDATE print_jobs SET started_at=:start WHERE id=:id'),
                                     {'start':history['started_at'], 'id':self._current_job_db_id})

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
                        from core.job_terminal_observations import terminal_event
                        timeline_item = terminal_event(conn, self._current_job_db_id) if (result.get('physical_ended') or result.get('transitioned')) else None
                        conn.commit()
                        from core.job_timeline import enqueue
                        if timeline_item is not None:enqueue(timeline_item)
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
                from core.job_terminal_observations import claim_terminal, terminal_event
                # As on origin/main, a Moonraker cancellation alerts as a failure.
                claim_terminal(conn, self._current_job_db_id,
                               alert_job_id=row[0] if row and row[0] and ended else None,
                               alert_cancelled=True)
                timeline_item=terminal_event(conn, self._current_job_db_id)
                reconcile_job_write(conn, production_orders)
            from core.job_timeline import enqueue
            if timeline_item is not None:enqueue(timeline_item)
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

            from core.job_terminal_observations import dispatch_terminal_alert
            # Dispatch alerts
            job_name = self._last_filename or "Unknown"
            if end_status == "completed":
                dispatch_terminal_alert(self._current_job_db_id, printer_events.dispatch_alert,
                    attempt_uid=timeline_item['attempt'] if timeline_item else None,
                    alert_type=AlertType.PRINT_COMPLETE,
                    severity=AlertSeverity.INFO,
                    job_id=row[0] if row else None,
                    title=f"Print Complete: {job_name}",
                    message=f"Finished on {self.name}",
                    printer_id=self.printer_id,
                )
            else:
                dispatch_terminal_alert(self._current_job_db_id, printer_events.dispatch_alert,
                    attempt_uid=timeline_item['attempt'] if timeline_item else None,
                    alert_type=AlertType.PRINT_CANCELLED if end_status == "cancelled" else AlertType.PRINT_FAILED,
                    severity=AlertSeverity.CRITICAL,
                    job_id=row[0] if row else None,
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
        from modules.printers.monitors.moonraker_monitor import (PROGRESS_DB_INTERVAL, engine, log, text, time)
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
        from modules.printers.monitors.moonraker_monitor import (log, time)
        try:
            time.sleep(5)
            from modules.printers import dispatch
            dispatch.attempt_dispatch(self.printer_id)
        except Exception as e:
            log.warning(f"[{self.name}] Dispatch attempt error: {e}")

    # ==================== Filament Slot Sync ====================

    def _maybe_sync_ace(self, ace, now):
        """Persist on change, plus the regular slot cadence; not every 3 s poll."""
        from modules.printers.monitors.moonraker_monitor import engine, SLOT_SYNC_INTERVAL
        if ace == getattr(self, '_last_ace', None) and now - getattr(self, '_last_ace_sync', 0) < SLOT_SYNC_INTERVAL:
            return
        if self._sync_ace(ace):
            self._last_ace, self._last_ace_sync = ace, now

    def _sync_ace(self, ace):
        from modules.printers.monitors.moonraker_monitor import engine, log
        from modules.printers.monitors.ams_inventory import sync_ace_inventory
        try:
            with engine.begin() as conn:
                sync_ace_inventory(conn, self.printer_id, ace)
            return True
        except Exception:
            log.warning('ACE inventory persistence failed; retry on next observation')
            return False

    def _sync_filament_slots(self, slots):
        """Sync MMU/ACE gate data to filament_slots DB table."""
        from modules.printers.monitors.moonraker_monitor import (engine, log, slot_filament_value, sql, text)
        try:
            with engine.begin() as conn:
                from core.interfaces.inventory_moves import inventory_moves
                inventory_moves().observe_empty_slots(conn, self.printer_id,
                    # Only an explicit empty gate is departure evidence; unknown or
                    # buffered gates keep their spool.
                    [s.gate + 1 for s in slots if getattr(s, 'gate_status', None) == 0])
                for slot in slots:
                    slot_num = slot.gate + 1  # DB uses 1-based slot numbers
                    material_key = (slot.material or "").upper()
                    filament_type = slot_filament_value(material_key)
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
                        text("DELETE FROM filament_slots WHERE printer_id=:pid AND slot_number>:ms AND do_not_feed IS NOT TRUE"),
                        {"pid": self.printer_id, "ms": max_slot})
        except Exception as e:
            log.debug(f"[{self.name}] Filament slot sync: {e}")

    # ==================== Job Auto-Linking ====================

    def _try_auto_link(self, filename: str, total_layers: int):
        """
        Try to link an unreserved Moonraker print to current generic work.
        Same two-strategy approach as Bambu monitor:
          1. Name match
          2. Layer count fingerprint
        """
        from modules.printers.monitors.moonraker_monitor import (engine, log, prepare_job_write, text)
        try:
            with engine.begin() as conn:
                from core.interfaces.education_policy import is_education_reserved_filename
                if is_education_reserved_filename(filename):
                    return
                from core.dispatch_observations import generic_observation_reserved
                if generic_observation_reserved(conn, printer_id=self.printer_id, observed_filename=filename):
                    return
                from core.dispatch_payload_selection import SELECTED_PRINT_FILE_JOIN_SQL
                candidates = conn.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: the SELECTED_PRINT_FILE_JOIN_SQL module constant; values are bound
                    f"""
                    SELECT j.id as job_id, j.status, pf.filename, pf.original_filename,
                           pf.layer_count, m.name as model_name
                    FROM jobs j
                    JOIN models m ON j.model_id = m.id
                    LEFT JOIN education_submissions s ON s.job_id = j.id
                    JOIN print_files pf ON {SELECTED_PRINT_FILE_JOIN_SQL}
                    WHERE j.printer_id = :pid
                      AND j.status IN ('printing', 'scheduled', 'pending')
                      AND COALESCE(j.hold,false)=false
                      AND NOT EXISTS (SELECT 1 FROM dispatch_attempt_receipts r WHERE r.job_id=j.id)
                      AND NOT EXISTS (SELECT 1 FROM dispatch_recovery_history h WHERE h.job_id=j.id)
                      AND NOT EXISTS (
                          SELECT 1 FROM education_submissions s WHERE s.job_id=j.id
                      )
                      AND NOT EXISTS (SELECT 1 FROM print_jobs p WHERE p.scheduled_job_id=j.id
                                      AND p.status IN ('running','printing','paused'))
                    ORDER BY CASE WHEN j.status='printing' THEN 0 ELSE 1 END, j.id
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

                    printing_matches = {key: value for key, value in layer_matches.items()
                                        if value['status'] == 'printing'}
                    if printing_matches:
                        layer_matches = printing_matches
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
        """Reconcile current generic work without granting dispatch authority."""
        from modules.printers.monitors.moonraker_monitor import (prepare_job_write, reconcile_job_write, text)
        if not self._current_job_db_id:
            return False
        from core.order_production import (prepare_job_write, reconcile_job_write, prepare_print_job_write,
                                   ProductionLinkageChanged, retry_production_linkage)
        parents = prepare_job_write(conn, [scheduled_job_id])
        # Parent -> print record -> job is the same order as terminal handlers.
        print_query = text("SELECT printer_id,scheduled_job_id,status FROM print_jobs WHERE id=:id")
        if conn.dialect.name == 'postgresql':
            print_query = text(str(print_query) + " FOR UPDATE")  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: literal ' FOR UPDATE' appended to a literal statement
        current = conn.execute(print_query, {'id':self._current_job_db_id}).first()
        if (not current or current[0] != self.printer_id or current[2] not in ('running', 'printing')
                or current[1] not in (None, scheduled_job_id)):
            return False
        from core.dispatch_observations import generic_observation_reserved
        observed_name = conn.execute(text('SELECT filename FROM print_jobs WHERE id=:id'),
                                     {'id': self._current_job_db_id}).scalar_one()
        from core.interfaces.education_policy import is_education_reserved_filename
        if is_education_reserved_filename(observed_name):
            return False
        if generic_observation_reserved(conn, printer_id=self.printer_id, observed_filename=observed_name):
            return False
        from core.scheduler_payload_authority import capture
        from core.dispatch_identity import ownership_refusal
        snapshot = capture(conn, scheduled_job_id)
        printer = conn.execute(text('SELECT org_id,shared,is_active FROM printers WHERE id=:id'),
                               {'id': self.printer_id}).mappings().first()
        job = conn.execute(text('SELECT hold FROM jobs WHERE id=:id'), {'id': scheduled_job_id}).first()
        if (snapshot is None
                or printer is None or not printer['is_active'] or not job or job[0]
                or ownership_refusal({'charged_to_org_id': snapshot['charged_to_org_id'],
                                      'model_org_id': snapshot['model_org_id'],
                                      'print_file_org_id': snapshot['selected_org_id']},
                                     {'org_id': printer['org_id'], 'shared': bool(printer['shared'])})):
            return False
        from core.registry import registry
        policy = registry.get_provider('EducationPolicyProvider')
        if policy is None:
            return False
        policy.assert_generic_job_mutation_allowed(conn, scheduled_job_id)
        policy.assert_generic_model_reference_allowed(conn, snapshot['model_id'])
        if conn.execute(text("SELECT 1 FROM print_jobs WHERE scheduled_job_id=:job AND id<>:id "
                             "AND status IN ('running','printing','paused')"),
                        {'job': scheduled_job_id, 'id': self._current_job_db_id}).first():
            return False
        from core.print_attempts import heuristic_link_state, remember_heuristic_link, sqlalchemy_runner
        previous_state = heuristic_link_state(sqlalchemy_runner(conn),scheduled_job_id)
        from modules.jobs.services import transition_job
        changed = transition_job(conn, scheduled_job_id, 'printing', 'monitor',
            from_states=('printing', 'scheduled', 'pending'),
            where="printer_id=:pid AND COALESCE(hold,false)=false"
                  " AND NOT EXISTS (SELECT 1 FROM dispatch_attempt_receipts r WHERE r.job_id=jobs.id)"
                  " AND NOT EXISTS (SELECT 1 FROM dispatch_recovery_history h WHERE h.job_id=jobs.id)"
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
        remember_heuristic_link(sqlalchemy_runner(conn),self._current_job_db_id,scheduled_job_id,previous_state)
        reconcile_job_write(conn, parents)
        return True

    def _auto_deduct_filament(self, conn, scheduled_job_id: int, *, observed_grams=None):
        """Account for one unambiguous loaded spool using actual schema fields.

        Klipper's measured extrusion is preferred. A stored sliced weight is an
        estimate fallback. Multi-spool attribution requires explicit mapping;
        never guess which spool supplied a multi-material print.
        """
        from modules.printers.monitors.moonraker_monitor import (log, text)
        import math
        job = conn.execute(text("""
            SELECT j.printer_id,j.charged_to_org_id,pf.filament_weight_grams,pf.filaments_json
            FROM jobs j LEFT JOIN print_files pf ON j.print_file_id=pf.id WHERE j.id=:id
        """), {'id':scheduled_job_id}).first()
        if not job or job[0] != self.printer_id:
            return
        from core.interfaces.filament_ownership import uses_external_filament_accounting
        if uses_external_filament_accounting(self.printer_id):
            log.info("Printer %s job %s automatic filament deduction skipped: external_accounting",
                     self.printer_id, scheduled_job_id)
            return
        from modules.printers.dispatch_support import is_kobra_s1
        printer = conn.execute(text('SELECT api_type,model,machine_type FROM printers WHERE id=:id'),
                               {'id': self.printer_id}).mappings().first()
        if printer and is_kobra_s1(printer):
            return self._deduct_s1_filament(conn, scheduled_job_id, observed_grams=observed_grams)
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

    def _deduct_s1_filament(self, conn, scheduled_job_id, *, observed_grams):
        """The stored mapping names the ACE slot, as in operator completion; never a located spool."""
        from modules.printers.monitors.moonraker_monitor import log, text
        import json
        import math
        from core.filament_ledger import flag_reconciliation, record_job_filament
        from core.filament_sources import attribute_source_grams, indexed_materials
        from core.print_attempts import sqlalchemy_runner
        from core.print_file_binding import job_file_id
        from core.tray_identity import decode
        from modules.printers.kobra_s1 import S1Refusal, stored_or_file_materials
        job = conn.execute(text('SELECT printer_id,charged_to_org_id,model_id,print_file_id,ams_mapping FROM jobs WHERE id=:id'),
                           {'id': scheduled_job_id}).mappings().first()
        if not job or job['printer_id'] != self.printer_id:
            return None
        file_id = job_file_id(conn, job['model_id'], job['print_file_id'])
        file = conn.execute(text('SELECT filaments_json,stored_path,filament_weight_grams FROM print_files WHERE id=:id'),
                            {'id': file_id}).mappings().first() if file_id else None
        sources = None
        if file:
            try:
                sources = indexed_materials(stored_or_file_materials(file['filaments_json'], file['stored_path']))
            except S1Refusal:
                pass
        def known(value):
            # A deterministic raise here would retry the print's end on every poll.
            try:
                return type(value) is not bool and math.isfinite(float(value)) and float(value) >= 0
            except (TypeError, ValueError):
                return False
        slot_grams = {}
        if sources and len(sources) == 1:
            source, material = next(iter(sources.items()))
            # Klipper's extrusion is measured; the file's weight is the sliced estimate.
            grams = next((g for g in (observed_grams, material.get('used_grams'), file['filament_weight_grams']) if known(g)), None)
            slot_grams = {source + 1: float(grams)} if grams is not None else {}
        elif sources and all(known(m.get('used_grams')) for m in sources.values()):
            # A measured total can't be split between sources, so each uses its sliced weight.
            slot_grams = {source + 1: float(m['used_grams']) for source, m in sources.items()}
        try:
            mapping = json.loads(job['ams_mapping']) if isinstance(job['ams_mapping'], str) else job['ams_mapping']
        except (ValueError, TypeError):
            mapping = None
        slots = {}
        for row in conn.execute(text("""
                SELECT fs.slot_number,fs.tray_observation,s.id AS spool_id FROM filament_slots fs
                LEFT JOIN spools s ON s.id=fs.assigned_spool_id AND s.status='active' AND (s.org_id IS NULL OR s.org_id=:org)
                WHERE fs.printer_id=:printer"""), {'org': job['charged_to_org_id'], 'printer': self.printer_id}).mappings():
            slots[row['slot_number']] = (row['spool_id'], decode(row['tray_observation']).get('pending'))
        grams_by_spool, _, unattributed = attribute_source_grams(slot_grams, mapping=mapping, mapped=True, slots=slots)
        if not slot_grams:
            unattributed = [dict(reason='unknown_material_quantity')]
        if unattributed:
            from core.audit import log_audit
            from sqlalchemy.orm import Session
            with Session(bind=conn, join_transaction_mode='rollback_only') as db:
                log_audit(db, 'filament.unattributed', 'job', scheduled_job_id, {'consumption': unattributed})
                # rollback_only joins the caller transaction: commit flushes the audit
                # without committing the monitor's Connection.
                db.commit()
            return None
        print_job_id = getattr(self, '_current_job_db_id', None)
        outcome = record_job_filament(sqlalchemy_runner(conn), scheduled_job_id, source='moonraker', grams_by_spool=grams_by_spool,
                                      print_job_id=print_job_id, notes='ACE slot from the stored mapping')
        if outcome.unattributed and print_job_id is not None:
            log.error("[%s] Filament for print %s of job %s not deducted (%s); reconciliation required",
                      getattr(self, 'name', self.printer_id), print_job_id, scheduled_job_id, outcome.reason)
            flag_reconciliation(sqlalchemy_runner(conn), scheduled_job_id, print_job_id,
                                reason=outcome.reason, source="moonraker")
        return outcome
