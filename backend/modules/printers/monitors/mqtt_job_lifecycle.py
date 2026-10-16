"""
Job lifecycle logic for Bambu MQTT monitor.

Extracted from PrinterMonitor to keep the connection class focused on
MQTT state management. Functions return new state values rather than
mutating self — PrinterMonitor._job_started() and _job_ended() are thin
wrappers that assign the returned values.
"""

import logging
import time
from datetime import datetime, timezone
from threading import Thread
from typing import Optional, Tuple, Dict, Any

from core.order_production import (prepare_job_write, reconcile_job_write, prepare_print_job_write,
                                   ProductionLinkageChanged, retry_production_linkage)
# Resolved at call time: never a stale get_db bound at import.
from core.db_live import live_get_db as get_db
from core.db_compat import as_utc_datetime, sql

from modules.archives.services import capture_attempt_spools

log = logging.getLogger('mqtt_monitor')


def _education_policy():
    from core.registry import registry

    return registry.get_provider("EducationPolicyProvider")


def load_mqtt_start_weights(conn,print_job_id):
    """Reuse recorded first-start evidence; historical absence stays unknown."""
    import json
    row=conn.execute('SELECT spool_snapshot FROM print_jobs WHERE id=?',(print_job_id,)).fetchone()
    try:
        stored=json.loads(row[0]).get('mqtt_start_weights',{}) if row and row[0] else {}
        return {int(identifier):snapshot for identifier,snapshot in stored.items()}
    except (ValueError,TypeError,AttributeError):return {}


def capture_mqtt_start_weights(conn,print_job_id,printer_id,state):
    """Extend only this newly captured first-start snapshot, never resume evidence."""
    import json,math
    row=conn.execute('SELECT spool_snapshot FROM print_jobs WHERE id=? AND printer_id=?',(print_job_id,printer_id)).fetchone()
    if row is None or row[0] is None:return {}
    snapshot=json.loads(row[0])
    if 'mqtt_start_weights' in snapshot:return load_mqtt_start_weights(conn,print_job_id)
    weights={}
    owner=snapshot.get('printer_org_id')
    for captured in snapshot.get('slots',[]):
        slot=captured.get('slot_number');identifier=captured.get('spool_id')
        if (type(owner) is not int or owner<=0 or type(slot) is not int or slot<0
                or type(identifier) is not int or identifier<=0
                or captured.get('spool_existed') is not True or captured.get('spool_org_id')!=owner):continue
        current=conn.execute('SELECT s.initial_weight_g,s.org_id FROM filament_slots fs JOIN spools s ON s.id=fs.assigned_spool_id WHERE fs.printer_id=? AND fs.slot_number=? AND s.id=?',(printer_id,slot,identifier)).fetchall()
        if len(current)!=1 or current[0][1]!=owner:continue
        initial=current[0][0]
        remain=state.get('_ams_remain',{}).get(slot)
        if (type(initial) in (int,float) and math.isfinite(initial) and initial>0
                and type(remain) in (int,float) and math.isfinite(remain) and 0<=remain<=100):
            weights[identifier]={'remain_pct':remain,'initial_weight_g':initial,'slot':slot}
    snapshot['mqtt_start_weights']=weights
    changed=conn.execute("UPDATE print_jobs SET spool_snapshot=? WHERE id=? AND printer_id=? AND status='running' AND spool_snapshot=?",(json.dumps(snapshot,sort_keys=True,separators=(',',':')),print_job_id,printer_id,row[0]))
    if changed.rowcount!=1:raise RuntimeError('First-start spool snapshot changed')
    return weights


def _unschedule(cur, job_id, printer_id) -> bool:
    """Bump/sweep one scheduled job back to pending through the state machine (pin kept)."""
    from modules.jobs.services import transition_job
    return transition_job(cur, job_id, 'pending', 'monitor', extra_fields={
        'printer_id': None, 'scheduled_start': None, 'scheduled_end': None, 'match_score': None},
        where='printer_id=:pid', params={'pid': printer_id})


@retry_production_linkage
def record_job_started(
    printer_id: int,
    printer_name: str,
    state: dict,
    dispatch_alert_fn,
    trigger_reschedule_fn,
) -> Tuple[Optional[int], Optional[int], dict]:
    """
    Record a new print job starting and link to a scheduled job if found.

    Args:
        printer_id: DB printer ID.
        printer_name: Display name for logging.
        state: Current MQTT state dict (self._state snapshot).
        dispatch_alert_fn: Callable matching PrinterMonitor._dispatch_alert signature.
        trigger_reschedule_fn: Callable to fire scheduler re-run.

    Returns:
        (new_job_id, linked_job_id, start_spool_weights)
        new_job_id is None on failure.
    """
    job_name = state.get('subtask_name', 'Unknown')
    filename = state.get('gcode_file', '')
    mqtt_job_id = state.get('job_id') or f"local_{int(time.time())}"
    total_layers = state.get('total_layer_num', 0)
    bed_target = state.get('bed_target_temper')
    nozzle_target = state.get('nozzle_target_temper')

    # Deferred actions — dispatched AFTER the transaction commits
    pending_alerts = []
    needs_reschedule = False
    new_job_id = None
    linked_job_id = None
    start_weights = {}

    try:
        with get_db() as conn:
            conn.isolation_level = None
            cur = conn.cursor()
            try:
                # Grab write lock upfront so SELECT->UPDATE sequences are atomic
                cur.execute("BEGIN IMMEDIATE")

                from core.attempt_start_observation import observe_start
                from core.dispatch_observations import publish_observed_start
                observed=observe_start(conn,printer_id=printer_id,protocol='bambu',
                    filename=filename,job_name=job_name,physical_values={
                        'job_id':str(mqtt_job_id),
                        'started_at':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                        'total_layers':total_layers or None,'bed_temp_target':bed_target,
                        'nozzle_temp_target':nozzle_target},source='bambu-mqtt-first-start',prepare_operational=True)
                new_job_id=observed.id
                if observed.education_sensitive or observed.reserved or not observed.fresh:
                    linked_job_id=observed.linked_job_id
                    start_weights=(capture_mqtt_start_weights(conn,new_job_id,printer_id,state)
                                   if observed.fresh and not observed.education_sensitive
                                   else load_mqtt_start_weights(conn,new_job_id) if not observed.education_sensitive else {})
                    conn.execute('COMMIT')
                    if observed.fresh and not observed.education_sensitive:
                        publish_observed_start(printer_id,new_job_id,job_name)
                    return new_job_id,linked_job_id,start_weights
                candidate_ids=list(observed.candidate_ids)
                production_orders=list(observed.production_orders)
                candidate_placeholders = ','.join('?' for _ in candidate_ids) or '?'
                candidate_parameters = candidate_ids or [0]

                # ---- Stale schedule cleanup ----
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
                cur.execute(f"""
                    SELECT id, item_name FROM jobs
                    WHERE printer_id = ? AND status = 'scheduled'
                      AND scheduled_start < {sql.now_offset('-2 hours')}
                      AND id IN ({candidate_placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM education_submissions s WHERE s.job_id=jobs.id
                      )
                """, [printer_id, *candidate_parameters])
                stale_rows = cur.fetchall()
                if stale_rows:
                    stale_ids = [r[0] for r in stale_rows]
                    stale_names = [r[1] or f"job #{r[0]}" for r in stale_rows]
                    changed_ids = {jid for jid in stale_ids if _unschedule(cur, jid, printer_id)}
                    stale_names = [name for row_id,name in zip(stale_ids,stale_names) if row_id in changed_ids]
                    stale_ids = sorted(changed_ids)
                    if stale_ids:
                        log.info(f"[{printer_name}] Swept {len(stale_ids)} stale scheduled job(s): {', '.join(stale_names)}")
                        pending_alerts.append(dict(
                            alert_type='schedule_bump', severity='info',
                            title=f"Stale schedule swept on {printer_name}",
                            message=f"Jobs reset to pending (past 2hr window): {', '.join(stale_names)}",
                            metadata={"bumped_job_ids": stale_ids, "reason": "stale_schedule"}
                        ))
                        needs_reschedule = True

                # ---- Job matching (3 strategies) ----
                cur.execute(f"""
                    SELECT DISTINCT j.id, pf.filename, j.item_name, m.name as model_name,
                           pf.layer_count, j.status, j.scheduled_start
                    FROM jobs j
                    LEFT JOIN models m ON j.model_id = m.id
                    LEFT JOIN print_files pf ON m.id = pf.model_id
                    WHERE j.printer_id = ?
                    AND j.status IN ('scheduled', 'pending')
                    AND j.id IN ({candidate_placeholders})
                    AND NOT EXISTS (
                        SELECT 1 FROM education_submissions s WHERE s.job_id=j.id
                    )
                    ORDER BY j.scheduled_start ASC
                    LIMIT 10
                """, [printer_id, *candidate_parameters])
                candidates = cur.fetchall()
                job_base = job_name.lower().replace('.3mf', '').replace('.gcode', '')

                # Strategy 1: Name matching
                for cand_id, cand_filename, cand_item_name, cand_model_name, cand_layers, cand_status, cand_sched in candidates:
                    match_targets = []
                    if cand_filename:
                        match_targets.append(cand_filename.lower().replace('.3mf', '').replace('.gcode', ''))
                    if cand_item_name:
                        match_targets.append(cand_item_name.lower())
                    if cand_model_name:
                        match_targets.append(cand_model_name.lower())

                    for target in match_targets:
                        if target in job_base or job_base in target:
                            linked_job_id = cand_id
                            log.info(f"[{printer_name}] Linked to job {cand_id} by name ('{job_base}' ~ '{target}')")
                            break

                    if linked_job_id:
                        break

                # Strategy 2: Layer count matching
                if not linked_job_id and total_layers > 0:
                    layer_matches = list({c[0]: (c[0], c[3], c[4]) for c in candidates if c[4] == total_layers}.values())
                    if len(layer_matches) == 1:
                        linked_job_id = layer_matches[0][0]
                        log.info(f"[{printer_name}] Linked to job {linked_job_id} by layer count ({total_layers} layers)")
                    elif len(layer_matches) > 1:
                        log.info(f"[{printer_name}] {len(layer_matches)} jobs match {total_layers} layers - cannot auto-link")

                # Strategy 3: Sole scheduled candidate within ±2hr window
                if not linked_job_id:
                    now_utc = datetime.now(timezone.utc)
                    window_candidates = []
                    for cand_id, cand_filename, cand_item_name, cand_model_name, cand_layers, cand_status, cand_sched in candidates:
                        if cand_status != 'scheduled' or not cand_sched:
                            continue
                        # Layer count sanity check
                        if total_layers and cand_layers and total_layers > 0 and cand_layers > 0:
                            ratio = max(total_layers, cand_layers) / min(total_layers, cand_layers)
                            if ratio > 1.2:
                                continue
                        try:
                            sched_dt = datetime.fromisoformat(cand_sched)
                            sched_dt = (sched_dt.replace(tzinfo=timezone.utc) if sched_dt.tzinfo is None
                                        else sched_dt.astimezone(timezone.utc))
                            if abs((now_utc - sched_dt).total_seconds()) <= 7200:
                                window_candidates.append(cand_id)
                        except (ValueError, TypeError):
                            continue
                    if len(window_candidates) == 1:
                        linked_job_id = window_candidates[0]
                        log.info(f"[{printer_name}] Linked to job {linked_job_id} by sole scheduled candidate (±2hr window)")
                    elif len(window_candidates) > 1:
                        log.info(f"[{printer_name}] {len(window_candidates)} scheduled jobs in ±2hr window - cannot auto-link")

                if linked_job_id:
                    from modules.jobs.services import transition_job, Expr
                    changed = transition_job(cur, linked_job_id, 'printing', 'monitor',
                        from_states=('scheduled', 'pending'),
                        extra_fields={'actual_start': Expr('COALESCE(actual_start, :started)',
                            started=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))},
                        where="printer_id=:pid AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=jobs.id)",
                        params={'pid': printer_id})
                    if not changed:
                        linked_job_id = None
                    else:
                        # Persist the match so the print's end completes this
                        # job instead of auto-creating a duplicate one.
                        cur.execute(
                            "UPDATE print_jobs SET scheduled_job_id = ? WHERE id = ? AND scheduled_job_id IS NULL",
                            (linked_job_id, new_job_id))
                        if cur.rowcount == 1:
                            from core.print_attempts import bind_print_attempt, qmark_runner
                            bind_print_attempt(qmark_runner(cur), new_job_id, linked_job_id)

                # ---- Ad-hoc print: bump displaced scheduled jobs ----
                if not linked_job_id and candidates:
                    log.info(f"[{printer_name}] No auto-match for '{job_base}' ({total_layers} layers) - ad-hoc print")
                    cur.execute(f"""
                        SELECT id, item_name FROM jobs
                        WHERE printer_id = ? AND status = 'scheduled'
                          AND id IN ({candidate_placeholders})
                          AND NOT EXISTS (
                              SELECT 1 FROM education_submissions s WHERE s.job_id=jobs.id
                          )
                    """, [printer_id, *candidate_parameters])
                    displaced = cur.fetchall()
                    if displaced:
                        displaced_ids = [r[0] for r in displaced]
                        displaced_names = [r[1] or f"job #{r[0]}" for r in displaced]
                        changed_ids = {jid for jid in displaced_ids if _unschedule(cur, jid, printer_id)}
                        displaced_names = [name for row_id,name in zip(displaced_ids,displaced_names) if row_id in changed_ids]
                        displaced_ids = sorted(changed_ids)
                        if displaced_ids:
                            log.info(f"[{printer_name}] Bumped {len(displaced_ids)} scheduled job(s) for ad-hoc print: {', '.join(displaced_names)}")
                            pending_alerts.append(dict(
                                alert_type='schedule_bump', severity='info',
                                title=f"Scheduled jobs bumped on {printer_name}",
                                message=f"Ad-hoc print displaced: {', '.join(displaced_names)}. Jobs reset to pending.",
                                metadata={"bumped_job_ids": displaced_ids, "reason": "ad_hoc_print", "ad_hoc_file": job_name}
                            ))
                            needs_reschedule = True
                elif not linked_job_id:
                    log.info(f"[{printer_name}] No auto-match for '{job_base}' ({total_layers} layers) - no candidates to bump")

                # Fresh attempt/snapshot already prepared in the same outer
                # transaction; fuzzy queue matching remains operational only.
                start_weights=capture_mqtt_start_weights(conn,new_job_id,printer_id,state)

                reconcile_job_write(conn, production_orders)

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception as e:
                    log.debug(f"Failed to rollback transaction: {e}")
                raise

        log.info(f"[{printer_name}] Job started: {job_name} (DB id: {new_job_id})")

        from core.dispatch_observations import publish_observed_start
        publish_observed_start(printer_id,new_job_id,job_name)
        # Dispatch deferred alerts and reschedule (outside transaction)
        for alert_kwargs in pending_alerts:
            dispatch_alert_fn(**alert_kwargs)
        if needs_reschedule:
            trigger_reschedule_fn()

        return new_job_id, linked_job_id, start_weights

    except ProductionLinkageChanged:
        raise
    except Exception as failure:
        log.error("[%s] Failed to record job start (%s); reconciliation required",
                  printer_name,type(failure).__name__)
        return None, None, {}


@retry_production_linkage
def record_job_ended(
    printer_id: int,
    printer_name: str,
    current_job_id: int,
    linked_job_id: Optional[int],
    status: str,
    state: dict,
    start_spool_weights: dict,
    dispatch_alert_fn,
) -> Optional[int]:
    """
    Record a print job ending, update care counters, dispatch alerts.

    Args:
        printer_id: DB printer ID.
        printer_name: Display name for logging.
        current_job_id: print_jobs.id of the active job.
        linked_job_id: jobs.id of the linked scheduled job (may be None).
        status: 'completed', 'failed', or 'cancelled'.
        state: Current MQTT state dict snapshot.
        start_spool_weights: Spool weight snapshot from job start.
        dispatch_alert_fn: Callable matching PrinterMonitor._dispatch_alert signature.

    Returns:
        The final linked_job_id (may differ from input if a new jobs record was created).
    """
    import modules.notifications.event_dispatcher as printer_events
    error_code = state.get('print_error') if status == 'failed' else None
    committed_completion = False
    completion_job_id = None
    job_applied = False

    try:
        with get_db() as conn:
            cur = conn.cursor()

            # Guard: skip if job was already ended (e.g. by HMS error handler)
            existing = cur.execute(
                "SELECT status FROM print_jobs WHERE id = ?", (current_job_id,)
            ).fetchone()
            if existing and existing[0] in ('completed', 'failed', 'cancelled'):
                log.info(f"[{printer_name}] Job {current_job_id} already ended "
                         f"(status={existing[0]}), skipping duplicate end")
                return linked_job_id

            now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

            # Calculate duration
            pj_row = cur.execute("SELECT started_at FROM print_jobs WHERE id = ?",
                                 (current_job_id,)).fetchone()
            duration_hours = None
            duration_seconds = None
            if pj_row and pj_row[0]:
                try:
                    started = as_utc_datetime(pj_row[0])
                    ended = as_utc_datetime(now_utc)
                    duration_seconds = (ended - started).total_seconds()
                    duration_hours = round(duration_seconds / 3600, 4)
                except Exception as e:
                    log.debug(f"Failed to parse job duration: {e}")

            policy = _education_policy()
            if policy:
                education_result = policy.terminal_monitor_observation(
                    conn,
                    print_job_id=current_job_id,
                    printer_id=printer_id,
                    terminal_status=status,
                    ended_at=now_utc,
                    duration_seconds=duration_seconds,
                    error_code=str(error_code) if error_code else None,
                )
                if education_result.get("education_owned") or education_result.get(
                    "education_reserved"
                ):
                    conn.commit()
                    # The physical print completion is published whenever the print
                    # really ended -- also when Education refused the job transition
                    # (physical_ended) -- but names the job only when it applied.
                    applied = (education_result.get('authorized') is True
                               and education_result.get('transitioned') is True)
                    physical = applied or education_result.get('physical_ended') is True
                    committed_completion = status == 'completed' and physical
                    completion_job_id = education_result.get('job_id') if applied else None
                    log.info(
                        "[%s] Education job terminal policy result: %s",
                        printer_name,
                        education_result,
                    )
                    if physical:
                        try:
                            from modules.archives.archive import create_print_archive
                            create_print_archive(print_job_id=current_job_id, printer_id=printer_id,
                                                 success=(status == 'completed'),
                                                 result_status=status if status == 'cancelled' else None)
                        except Exception as ae:
                            log.warning(f"[{printer_name}] Failed to create print archive: {ae}")
                    return education_result.get("job_id") or linked_job_id

            linked_job_id, production_orders = prepare_print_job_write(conn, current_job_id)
            # The first read was only a hint. Another callback may have
            # finalized the print while this observation waited for its lock.
            locked_status = cur.execute('SELECT status FROM print_jobs WHERE id=?',
                                        (current_job_id,)).fetchone()
            if locked_status is None or locked_status[0] in ('completed','failed','cancelled'):
                return linked_job_id

            # Calculate filament used by comparing AMS remain percentages
            filament_used_g = None
            if start_spool_weights:
                try:
                    total_used = 0.0
                    for spool_id, snap in start_spool_weights.items():
                        slot_num = snap['slot']
                        end_pct = state.get('_ams_remain', {}).get(slot_num)
                        if end_pct is not None:
                            delta_pct = snap['remain_pct'] - end_pct
                            if delta_pct > 0:
                                total_used += snap['initial_weight_g'] * delta_pct / 100.0
                    if total_used > 0:
                        filament_used_g = round(total_used, 2)
                except Exception as e:
                    log.debug(f"[{printer_name}] Filament delta calc: {e}")

            # Update print_jobs record
            cur.execute("""
                UPDATE print_jobs
                SET ended_at = ?, status = ?, error_code = ?,
                    print_duration_seconds = ?, filament_used_g = ?
                WHERE id = ?
            """, (now_utc, status, error_code,
                  int(duration_seconds) if duration_seconds else None,
                  filament_used_g, current_job_id))

            job_name = state.get('subtask_name', 'Unknown')
            job_status = status.lower()

            if linked_job_id:
                # Keep the slicer estimate in duration_hours; the actual duration
                # is actual_end - actual_start. Only fill duration_hours when the
                # job carried no estimate at all.
                # An operator's terminal decision (e.g. Mark failed while the
                # print was still running) is never overwritten by the print end.
                from modules.jobs.services import transition_job, Expr
                job_applied = transition_job(cur, linked_job_id, job_status, 'monitor', extra_fields={
                    'actual_end': now_utc,
                    'actual_start': Expr('COALESCE(actual_start, :pj_started)', pj_started=pj_row[0] if pj_row else None),
                    'duration_hours': Expr('COALESCE(duration_hours, :measured)', measured=duration_hours)})
                if job_applied:
                    log.info(f"[{printer_name}] Updated linked job {linked_job_id} to '{job_status}' (duration={duration_hours}h)")
                else:
                    log.info(f"[{printer_name}] Linked job {linked_job_id} already terminal; kept operator status")
            else:
                job_applied = True
                # Create a jobs record for metrics tracking
                actual_start = pj_row[0] if pj_row else now_utc
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- values are bound and the only interpolated fragment is sql.returning_id()
                cur.execute(f"""
                    INSERT INTO jobs (item_name, printer_id, status, actual_start, actual_end,
                                     duration_hours, quantity, hold, is_locked, quantity_on_bed)
                    VALUES (?, ?, ?, ?, ?, ?, 1, FALSE, FALSE, 1)
                    {sql.returning_id()}
                """, (job_name, printer_id, job_status, actual_start, now_utc, duration_hours))
                if sql.is_postgres:
                    linked_job_id = cur.fetchone()[0]
                else:
                    linked_job_id = cur.lastrowid
                cur.execute("UPDATE print_jobs SET scheduled_job_id = ? WHERE id = ?",
                            (linked_job_id, current_job_id))
                from core.print_attempts import bind_print_attempt, qmark_runner
                bind_print_attempt(qmark_runner(cur), current_job_id, linked_job_id)
                log.info(f"[{printer_name}] Created job {linked_job_id} for '{job_name}' ({job_status}, duration={duration_hours}h)")

            reconcile_job_write(conn, production_orders)
            conn.commit()
            # The physical print completion is always published (archive capture,
            # energy recording, auto power-off are keyed by the print). Job-scoped
            # effects only see the job when this print end actually completed it.
            committed_completion = status == 'completed'
            completion_job_id = linked_job_id if job_applied else None

        log.info(f"[{printer_name}] Job {status}: DB id {current_job_id}")
        # Job-scoped alerts name the job only when this end applied to it; otherwise
        # they are printer-scoped, like a printer-only print's.
        alert_job_id = linked_job_id if job_applied else None

        # Lazy imports to avoid circular import at module load time
        from modules.notifications.alert_dispatch import dispatch_alert as _dispatch_alert_b
        from modules.notifications.alert_dispatch import _start_bed_cooled_monitor
        from modules.archives.archive import create_print_archive

        # Dispatch alerts via Path B (supports webhooks, push, email)
        if status == 'completed':
            _dispatch_alert_b(
                alert_type='print_complete',
                severity='info',
                title=f"Print Complete: {job_name} ({printer_name})",
                message=f"Job finished successfully on {printer_name}.",
                printer_id=printer_id,
                job_id=alert_job_id,
            )
            # Increment care counters
            try:
                with get_db() as conn2:
                    cur2 = conn2.cursor()
                    cur2.execute("SELECT started_at, ended_at FROM print_jobs WHERE id = ?", (current_job_id,))
                    row = cur2.fetchone()
                    if row and row[0] and row[1]:
                        started = as_utc_datetime(row[0])
                        ended = as_utc_datetime(row[1])
                        duration_sec = (ended - started).total_seconds()
                        printer_events.increment_care_counters(printer_id, duration_sec / 3600.0, 1)
            except Exception as ce:
                log.warning(f"[{printer_name}] Failed to update care counters: {ce}")

        elif status == 'failed':
            progress = state.get('mc_percent', 0)
            err = state.get('print_error')
            msg = f"Job failed on {printer_name} at {progress}% progress."
            if err:
                msg += f" Error code: {err}"
            _dispatch_alert_b(
                alert_type='print_failed',
                severity='critical',
                title=f"Print Failed: {job_name} ({printer_name})",
                message=msg,
                printer_id=printer_id,
                job_id=alert_job_id,
                metadata={"progress_percent": progress, "error_code": err}
            )
            printer_events.record_error(
                printer_id=printer_id,
                error_code=str(err) if err else "PRINT_FAILED",
                error_message=msg,
                source="bambu_job",
                severity="error",
                create_alert=False,
            )

        # Archive the print (completed, failed, or cancelled)
        try:
            create_print_archive(
                print_job_id=current_job_id,
                printer_id=printer_id,
                success=(status == 'completed'),
                result_status=status if status == 'cancelled' else None,
            )
        except Exception as ae:
            log.warning(f"[{printer_name}] Failed to create print archive: {ae}")

        # Start bed-cooled monitoring after successful completion
        if status == 'completed':
            try:
                _start_bed_cooled_monitor(printer_id, printer_name)
            except Exception as be:
                log.debug(f"[{printer_name}] Bed cooled monitor start failed: {be}")

        return linked_job_id

    except ProductionLinkageChanged:
        raise
    except Exception:
        log.error("[%s] Failed to record job end; reconciliation required", printer_name)
        return linked_job_id
    finally:
        from core import db as core_db
        from core.dispatch_claims import retire_observed_terminal
        try:
            retire_observed_terminal(core_db.engine,printer_id=printer_id,print_job_id=current_job_id)
        finally:
            if committed_completion:
                from modules.printers.committed_terminal_events import publish_committed_completion
                publish_committed_completion(printer_id, current_job_id, completion_job_id)
