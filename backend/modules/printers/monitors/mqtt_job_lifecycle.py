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

from core.db_utils import get_db

log = logging.getLogger('mqtt_monitor')


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

                # ---- Stale schedule cleanup ----
                cur.execute("""
                    SELECT id, item_name FROM jobs
                    WHERE printer_id = ? AND status = 'scheduled'
                      AND scheduled_start < datetime('now', 'localtime', '-2 hours')
                """, (printer_id,))
                stale_rows = cur.fetchall()
                if stale_rows:
                    stale_ids = [r[0] for r in stale_rows]
                    stale_names = [r[1] or f"job #{r[0]}" for r in stale_rows]
                    cur.execute(
                        "UPDATE jobs SET status = 'pending', printer_id = NULL,"
                        " scheduled_start = NULL, scheduled_end = NULL, match_score = NULL"
                        " WHERE id IN ({})".format(','.join('?' * len(stale_ids))),
                        stale_ids)
                    log.info(f"[{printer_name}] Swept {len(stale_ids)} stale scheduled job(s): {', '.join(stale_names)}")
                    pending_alerts.append(dict(
                        alert_type='schedule_bump', severity='info',
                        title=f"Stale schedule swept on {printer_name}",
                        message=f"Jobs reset to pending (past 2hr window): {', '.join(stale_names)}",
                        metadata={"bumped_job_ids": stale_ids, "reason": "stale_schedule"}
                    ))
                    needs_reschedule = True

                # ---- Job matching (3 strategies) ----
                cur.execute("""
                    SELECT DISTINCT j.id, pf.filename, j.item_name, m.name as model_name,
                           pf.layer_count, j.status, j.scheduled_start
                    FROM jobs j
                    LEFT JOIN models m ON j.model_id = m.id
                    LEFT JOIN print_files pf ON m.id = pf.model_id
                    WHERE j.printer_id = ?
                    AND j.status IN ('scheduled', 'pending')
                    ORDER BY j.scheduled_start ASC
                    LIMIT 10
                """, (printer_id,))
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
                    now_local = datetime.now()
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
                            if sched_dt.tzinfo is not None:
                                sched_dt = sched_dt.replace(tzinfo=None)
                            if abs((now_local - sched_dt).total_seconds()) <= 7200:
                                window_candidates.append(cand_id)
                        except (ValueError, TypeError):
                            continue
                    if len(window_candidates) == 1:
                        linked_job_id = window_candidates[0]
                        log.info(f"[{printer_name}] Linked to job {linked_job_id} by sole scheduled candidate (±2hr window)")
                    elif len(window_candidates) > 1:
                        log.info(f"[{printer_name}] {len(window_candidates)} scheduled jobs in ±2hr window - cannot auto-link")

                # ---- Ad-hoc print: bump displaced scheduled jobs ----
                if not linked_job_id and candidates:
                    log.info(f"[{printer_name}] No auto-match for '{job_base}' ({total_layers} layers) - ad-hoc print")
                    cur.execute("""
                        SELECT id, item_name FROM jobs
                        WHERE printer_id = ? AND status = 'scheduled'
                    """, (printer_id,))
                    displaced = cur.fetchall()
                    if displaced:
                        displaced_ids = [r[0] for r in displaced]
                        displaced_names = [r[1] or f"job #{r[0]}" for r in displaced]
                        cur.execute(
                            "UPDATE jobs SET status = 'pending', printer_id = NULL,"
                            " scheduled_start = NULL, scheduled_end = NULL, match_score = NULL"
                            " WHERE id IN ({})".format(','.join('?' * len(displaced_ids))),
                            displaced_ids)
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

                # ---- Insert print_jobs record ----
                cur.execute("""
                    INSERT INTO print_jobs
                    (printer_id, job_id, filename, job_name, started_at, status,
                     total_layers, bed_temp_target, nozzle_temp_target, scheduled_job_id)
                    VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """, (printer_id, str(mqtt_job_id), filename, job_name,
                      datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                      total_layers or None, bed_target, nozzle_target,
                      linked_job_id))
                new_job_id = cur.lastrowid

                # Snapshot spool weights at job start for filament usage calculation
                try:
                    spool_rows = cur.execute("""
                        SELECT fs.slot_number, s.id, s.remaining_weight_g
                        FROM filament_slots fs
                        JOIN spools s ON fs.assigned_spool_id = s.id
                        WHERE fs.printer_id = ? AND fs.assigned_spool_id IS NOT NULL
                    """, (printer_id,)).fetchall()
                    for slot, spool_id, weight in spool_rows:
                        if weight is not None:
                            start_weights[spool_id] = weight
                except Exception:
                    pass

                # Update linked job status to 'printing'
                if linked_job_id:
                    cur.execute(
                        "UPDATE jobs SET status = 'printing',"
                        " actual_start = COALESCE(actual_start, ?) WHERE id = ?",
                        (datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), linked_job_id))
                    log.info(f"[{printer_name}] Updated job {linked_job_id} status to 'printing'")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        log.info(f"[{printer_name}] Job started: {job_name} (DB id: {new_job_id})")

        # Dispatch deferred alerts and reschedule (outside transaction)
        for alert_kwargs in pending_alerts:
            dispatch_alert_fn(**alert_kwargs)
        if needs_reschedule:
            trigger_reschedule_fn()

        return new_job_id, linked_job_id, start_weights

    except Exception as e:
        log.error(f"[{printer_name}] Failed to record job start: {e}")
        return None, None, {}


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
                    started = datetime.fromisoformat(pj_row[0])
                    ended = datetime.fromisoformat(now_utc)
                    duration_seconds = (ended - started).total_seconds()
                    duration_hours = round(duration_seconds / 3600, 4)
                except Exception:
                    pass

            # Calculate filament used by comparing spool weights
            filament_used_g = None
            if start_spool_weights:
                try:
                    total_used = 0.0
                    spool_rows = cur.execute("""
                        SELECT s.id, s.remaining_weight_g FROM filament_slots fs
                        JOIN spools s ON fs.assigned_spool_id = s.id
                        WHERE fs.printer_id = ? AND fs.assigned_spool_id IS NOT NULL
                    """, (printer_id,)).fetchall()
                    for spool_id, current_weight in spool_rows:
                        if spool_id in start_spool_weights and current_weight is not None:
                            delta = start_spool_weights[spool_id] - current_weight
                            if delta > 0:
                                total_used += delta
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
                cur.execute("UPDATE jobs SET status = ?, actual_end = ?, duration_hours = COALESCE(?, duration_hours) WHERE id = ?",
                            (job_status, now_utc, duration_hours, linked_job_id))
                log.info(f"[{printer_name}] Updated linked job {linked_job_id} to '{job_status}' (duration={duration_hours}h)")
            else:
                # Create a jobs record for metrics tracking
                actual_start = pj_row[0] if pj_row else now_utc
                cur.execute("""
                    INSERT INTO jobs (item_name, printer_id, status, actual_start, actual_end,
                                     duration_hours, quantity, hold, is_locked, quantity_on_bed)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 0, 0, 1)
                """, (job_name, printer_id, job_status, actual_start, now_utc, duration_hours))
                linked_job_id = cur.lastrowid
                cur.execute("UPDATE print_jobs SET scheduled_job_id = ? WHERE id = ?",
                            (linked_job_id, current_job_id))
                log.info(f"[{printer_name}] Created job {linked_job_id} for '{job_name}' ({job_status}, duration={duration_hours}h)")

            conn.commit()

        log.info(f"[{printer_name}] Job {status}: DB id {current_job_id}")

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
                job_id=linked_job_id,
            )
            # Increment care counters
            try:
                with get_db() as conn2:
                    cur2 = conn2.cursor()
                    cur2.execute("SELECT started_at, ended_at FROM print_jobs WHERE id = ?", (current_job_id,))
                    row = cur2.fetchone()
                    if row and row[0] and row[1]:
                        started = datetime.fromisoformat(row[0])
                        ended = datetime.fromisoformat(row[1])
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
                job_id=linked_job_id,
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

    except Exception as e:
        log.error(f"[{printer_name}] Failed to record job end: {e}")
        return linked_job_id
