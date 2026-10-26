"""
Job lifecycle event handlers.

Provides job_started(), job_completed(), update_job_progress() and the
monitor compatibility wrappers used by PrusaLink and Elegoo monitors.
"""

from core.base import AlertType, AlertSeverity

import logging
from typing import Optional

from core.order_production import (prepare_job_write, reconcile_job_write, prepare_print_job_write,
                                   ProductionLinkageChanged, retry_production_linkage)
from core.db_utils import get_db
from modules.archives.services import capture_attempt_spools
from core.db_compat import sql
from core.event_bus import get_event_bus
from core.interfaces.event_bus import Event
from core import events as ev
from modules.notifications.printer_health import increment_care_counters
from modules.notifications.alert_dispatch import dispatch_alert, _start_bed_cooled_monitor
from modules.notifications.error_handling import record_error

log = logging.getLogger("printer_events")

# Per-printer active print_job_id tracking for monitors that don't track it themselves
_active_print_jobs = {}  # printer_id -> print_job_id


def _education_policy():
    """Resolve the required cross-module provider without importing its implementation."""
    try:
        from core.registry import registry

        return registry.get_provider("EducationPolicyProvider")
    except Exception as exc:
        log.error("Education monitor policy unavailable: %s", exc)
        return None


@retry_production_linkage
def job_started(
    printer_id: int,
    job_name: str,
    total_layers: int = None,
    scheduled_job_id: int = None,
):
    """
    Called when a print job starts on any printer.
    Returns the print_jobs.id for tracking.
    """
    try:
        with get_db() as conn:
            from core.attempt_start_observation import observe_start
            conn.execute('BEGIN IMMEDIATE')
            try:
                result=observe_start(conn,printer_id=printer_id,filename=job_name,job_name=job_name,
                    physical_values={'started_at':conn.execute('SELECT '+sql.now()).fetchone()[0],
                                     'total_layers':total_layers},source='printer-event-first-start')
                job_id=result.id
                education_result={'education_owned':result.education_sensitive,'education_reserved':result.education_sensitive}
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            if not result.fresh:
                return job_id

        from core.job_timeline import for_attempt
        for_attempt(job_id, "started")
        log.info(f"Job started on printer {printer_id}: {job_name} (print_jobs.id={job_id})")

        if education_result and (
            education_result.get("education_owned")
            or education_result.get("education_reserved")
        ):
            return job_id

        try:
            bus = get_event_bus()
            bus.publish(Event(
                event_type=ev.JOB_STARTED,
                source_module="notifications",
                data={
                    "printer_id": printer_id,
                    "job_name": job_name,
                    "print_job_id": job_id,
                },
            ))
        except Exception:
            log.warning("Committed print start event delivery failed; observation retained")
        return job_id

    except ProductionLinkageChanged:
        raise
    except Exception as e:
        log.error(f"Failed to record job start for printer {printer_id}: {e}")
        return None


@retry_production_linkage
def job_completed(
    printer_id: int,
    print_job_id: int,
    success: bool = True,
    duration_seconds: float = None,
    fail_reason: str = None,
):
    """
    Called when a print job finishes on any printer.
    Updates care counters if successful.
    Creates alerts for completion/failure.
    """
    status = "completed" if success else "failed"
    committed_completion = False
    completion_job_id = None
    completion_metadata = None

    try:
        with get_db() as conn:
            cur = conn.cursor()

            policy = _education_policy()
            if policy:
                education_result = policy.terminal_monitor_observation(
                    conn,
                    print_job_id=print_job_id,
                    printer_id=printer_id,
                    terminal_status=status,
                    duration_seconds=duration_seconds,
                    error_code=fail_reason,
                )
                if education_result.get("education_owned") or education_result.get(
                    "education_reserved"
                ):
                    from core.job_terminal_observations import terminal_event
                    timeline_item = terminal_event(conn, print_job_id) if (education_result.get('physical_ended') or education_result.get('transitioned')) else None
                    conn.commit()
                    from core.job_timeline import enqueue
                    if timeline_item is not None:enqueue(timeline_item)
                    # Physical completion whenever the print really ended (also when
                    # Education refused the job transition); the job only if applied.
                    applied = (education_result.get('authorized') is True
                               and education_result.get('transitioned') is True)
                    physical = applied or education_result.get('physical_ended') is True
                    committed_completion = success is True and physical
                    completion_job_id = education_result.get('job_id') if applied else None
                    if physical:
                        _archive_physical_end(printer_id, print_job_id, status)
                    return education_result

            # Update print_jobs record
            linked_id, production_orders = prepare_print_job_write(conn, print_job_id)
            current = cur.execute(
                'SELECT printer_id,status FROM print_jobs WHERE id=?', (print_job_id,)
            ).fetchone()
            if current is None or current[0] != printer_id or current[1] != 'running':
                return None

            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
                f"""UPDATE print_jobs SET
                    status = ?,
                    ended_at = {sql.now()}
                WHERE id = ?""",
                (status, print_job_id)
            )

            # Get job details for alert
            cur.execute(
                "SELECT job_name, scheduled_job_id, ended_at FROM print_jobs WHERE id = ?",
                (print_job_id,)
            )
            row = cur.fetchone()
            job_name = row[0] if row else "Unknown"
            scheduled_job_id = row[1] if row else None
            from core.dispatch_observations import _utc
            # jobs.actual_end is canonical naive UTC; PG physical ended_at is aware.
            ended_at = _utc(row[2]).replace(tzinfo=None)

            # Get printer name
            cur.execute("SELECT name, nickname FROM printers WHERE id = ?", (printer_id,))
            prow = cur.fetchone()
            printer_name = prow[1] or prow[0] if prow else f"Printer {printer_id}"

            # Update the linked job through the state machine: an observed end
            # never overwrites a terminal/unapproved/rejected job.
            job_applied = False
            if scheduled_job_id:
                from modules.jobs.services import transition_job
                job_applied = transition_job(cur, scheduled_job_id, status, 'monitor',
                                             extra_fields={'actual_end': ended_at})
                if job_applied and success:
                    # Charge the print's material and measured time to the
                    # owner's grams/hours quota in the completion transaction,
                    # only when this end actually completed the job.
                    from core.quota import record_print_consumption, qmark_runner
                    cur.execute("SAVEPOINT quota_consumption")
                    try:
                        record_print_consumption(
                            qmark_runner(cur), scheduled_job_id,
                            measured_hours=(duration_seconds / 3600.0) if duration_seconds else None,
                            attempt_key=f"print:{int(print_job_id)}")
                        cur.execute("RELEASE SAVEPOINT quota_consumption")
                    except Exception:
                        # Never drop an observed completion over accounting;
                        # the failure is logged loudly for reconciliation.
                        cur.execute("ROLLBACK TO SAVEPOINT quota_consumption")
                        log.exception("Quota consumption for job %s could not be recorded", scheduled_job_id)

            from core.job_terminal_observations import claim_terminal, terminal_event
            claim_terminal(conn, print_job_id, alert_job_id=scheduled_job_id if job_applied else None)
            timeline_item=terminal_event(conn, print_job_id)
            reconcile_job_write(conn, production_orders)
            conn.commit()
            from core.job_timeline import enqueue
            if timeline_item is not None:enqueue(timeline_item)
            # The physical print completion is always published (keyed by the print);
            # job-scoped effects only see the job when this end actually completed it.
            committed_completion = success is True
            completion_job_id = scheduled_job_id if job_applied else None
            completion_metadata = {'job_name': job_name, 'duration_seconds': duration_seconds}
        # Job-scoped alerts and events name the job only when this end applied to
        # it; otherwise they are printer-scoped, like a printer-only print's.
        alert_job_id = scheduled_job_id if job_applied else None

        # Increment care counters if successful
        if success and duration_seconds:
            print_hours = duration_seconds / 3600.0
            increment_care_counters(printer_id, print_hours, 1)

        from core.job_terminal_observations import dispatch_terminal_alert

        # Create alert
        if success:
            dispatch_terminal_alert(print_job_id, dispatch_alert,
                alert_type=AlertType.PRINT_COMPLETE,
                severity=AlertSeverity.INFO,
                title=f"Print Complete: {job_name}",
                message=f"Finished on {printer_name}",
                printer_id=printer_id,
                job_id=alert_job_id,
            )
        else:
            dispatch_terminal_alert(print_job_id, dispatch_alert,
                alert_type=AlertType.PRINT_FAILED,
                severity=AlertSeverity.CRITICAL,
                title=f"Print Failed: {job_name}",
                message=f"Failed on {printer_name}" + (f": {fail_reason}" if fail_reason else ""),
                printer_id=printer_id,
                job_id=alert_job_id,
            )

            # Also record as error
            record_error(
                printer_id=printer_id,
                error_code="PRINT_FAILED",
                error_message=fail_reason or "Print failed",
                source="job",
                severity="error",
                create_alert=False,  # Already created above
            )

        log.info(f"Job {status} on printer {printer_id}: {job_name}")

        if not success:
            bus = get_event_bus()
            bus.publish(Event(
                event_type=ev.JOB_FAILED,
                source_module="notifications",
                data={
                    "printer_id": printer_id,
                    "job_name": job_name,
                    "status": status,
                    "print_job_id": print_job_id,
                    "scheduled_job_id": alert_job_id,
                    "success": success,
                    "duration_seconds": duration_seconds,
                },
            ))

        # Start bed-cooled monitoring if enabled and print was successful
        if success:
            _start_bed_cooled_monitor(printer_id, printer_name)

    except ProductionLinkageChanged:
        raise
    except Exception:
        log.error("Failed to record job completion for printer %s; reconciliation required", printer_id)
    finally:
        from core import db as core_db
        from core.dispatch_claims import retire_observed_terminal
        try:
            retire_observed_terminal(core_db.engine,printer_id=printer_id,print_job_id=print_job_id)
        finally:
            if committed_completion:
                from core.committed_terminal_events import publish_committed_completion
                publish_committed_completion(printer_id, print_job_id, completion_job_id,
                                             metadata=completion_metadata, source_module='notifications')


def _archive_physical_end(printer_id, print_job_id, status):
    """Archive an Education print that physically ended, as the MQTT and Moonraker paths do.

    Generic prints reach the archive through the event bus; Education ends publish
    no generic fan-out, so they are archived directly (after the commit).
    """
    try:
        from modules.archives.archive import create_print_archive
        create_print_archive(print_job_id=print_job_id, printer_id=printer_id, success=(status == 'completed'),
                             result_status=status if status == 'cancelled' else None)
    except Exception as failure:
        log.warning("Failed to archive Education print %s (%s)", print_job_id, type(failure).__name__)


@retry_production_linkage
def job_cancelled(
    printer_id: int,
    print_job_id: int,
):
    """
    Called when a print job is cancelled on any printer.
    Updates print_jobs status and publishes JOB_CANCELLED event.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()

            policy = _education_policy()
            if policy:
                education_result = policy.terminal_monitor_observation(
                    conn,
                    print_job_id=print_job_id,
                    printer_id=printer_id,
                    terminal_status="cancelled",
                )
                if education_result.get("education_owned") or education_result.get(
                    "education_reserved"
                ):
                    from core.job_terminal_observations import terminal_event
                    timeline_item = terminal_event(conn, print_job_id) if (education_result.get('physical_ended') or education_result.get('transitioned')) else None
                    conn.commit()
                    from core.job_timeline import enqueue
                    if timeline_item is not None:enqueue(timeline_item)
                    if ((education_result.get("authorized") is True and education_result.get("transitioned") is True)
                            or education_result.get("physical_ended") is True):
                        _archive_physical_end(printer_id, print_job_id, "cancelled")
                    return education_result

            linked_id, production_orders = prepare_print_job_write(conn, print_job_id)
            current = cur.execute(
                "SELECT printer_id,status FROM print_jobs WHERE id=?", (print_job_id,)
            ).fetchone()
            if current is None or current[0] != printer_id or current[1] != "running":
                return None

            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
                f"""UPDATE print_jobs SET
                    status = 'cancelled',
                    ended_at = {sql.now()}
                WHERE id = ?""",
                (print_job_id,)
            )

            cur.execute(
                "SELECT job_name, scheduled_job_id, ended_at FROM print_jobs WHERE id = ?",
                (print_job_id,)
            )
            row = cur.fetchone()
            job_name = row[0] if row else "Unknown"
            scheduled_job_id = row[1] if row else None
            from core.dispatch_observations import _utc
            # jobs.actual_end is canonical naive UTC; PG physical ended_at is aware.
            ended_at = _utc(row[2]).replace(tzinfo=None)

            cur.execute("SELECT name, nickname FROM printers WHERE id = ?", (printer_id,))
            prow = cur.fetchone()
            printer_name = prow[1] or prow[0] if prow else f"Printer {printer_id}"

            job_applied = False
            if scheduled_job_id:
                from modules.jobs.services import transition_job
                job_applied = transition_job(cur, scheduled_job_id, 'cancelled', 'monitor',
                                             extra_fields={'actual_end': ended_at})
            alert_job_id = scheduled_job_id if job_applied else None

            from core.job_terminal_observations import claim_terminal, terminal_event
            claim_terminal(conn, print_job_id)
            timeline_item=terminal_event(conn, print_job_id)
            reconcile_job_write(conn, production_orders)
            conn.commit()
            from core.job_timeline import enqueue
            if timeline_item is not None:enqueue(timeline_item)

        dispatch_alert(
            alert_type=AlertType.PRINT_FAILED,
            severity=AlertSeverity.WARNING,
            title=f"Print Cancelled: {job_name}",
            message=f"Cancelled on {printer_name}",
            printer_id=printer_id,
            job_id=alert_job_id,
        )

        log.info(f"Job cancelled on printer {printer_id}: {job_name}")

        bus = get_event_bus()
        bus.publish(Event(
            event_type=ev.JOB_CANCELLED,
            source_module="notifications",
            data={
                "printer_id": printer_id,
                "job_name": job_name,
                "status": "cancelled",
                "print_job_id": print_job_id,
                "scheduled_job_id": alert_job_id,
                "success": False,
            },
        ))

    except ProductionLinkageChanged:
        raise
    except Exception:
        log.error("Failed to record job cancellation for printer %s; reconciliation required", printer_id)
    finally:
        from core import db as core_db
        from core.dispatch_claims import retire_observed_terminal
        retire_observed_terminal(core_db.engine,printer_id=printer_id,print_job_id=print_job_id)


def update_job_progress(
    print_job_id: int,
    progress_percent: int = None,
    remaining_minutes: int = None,
    current_layer: int = None,
):
    """Update progress for an active print job."""
    updates = []
    params = []

    if progress_percent is not None:
        updates.append("progress_percent = ?")
        params.append(progress_percent)
    if remaining_minutes is not None:
        updates.append("remaining_minutes = ?")
        params.append(remaining_minutes)
    if current_layer is not None:
        updates.append("current_layer = ?")
        params.append(current_layer)

    if not updates:
        return

    params.append(print_job_id)

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
                f"UPDATE print_jobs SET {', '.join(updates)} WHERE id = ?",
                params
            )
            conn.commit()
    except Exception as e:
        log.error(f"Failed to update job progress for {print_job_id}: {e}")


# =============================================================================
# MONITOR COMPATIBILITY FUNCTIONS
# These are called by prusalink_monitor.py and elegoo_monitor.py
# and wrap the core job_started/job_completed/update_job_progress functions.
# =============================================================================

def on_print_start(printer_id: int, filename: str, total_layers: int = None,
                   layer_count: int = None, remaining_min: int = None):
    """Called by PrusaLink/Elegoo monitors when a print starts."""
    pj_id = job_started(
        printer_id=printer_id,
        job_name=filename,
        total_layers=total_layers or layer_count,
    )
    if pj_id:
        _active_print_jobs[printer_id] = pj_id


def on_print_complete(printer_id: int, filename: str,
                      duration_seconds: float = None, filament_used_g: float = None):
    """Called by PrusaLink/Elegoo monitors when a print completes."""
    pj_id = _active_print_jobs.pop(printer_id, None) or _resolve_education_print_job(printer_id, filename)
    if pj_id:
        job_completed(
            printer_id=printer_id,
            print_job_id=pj_id,
            success=True,
            duration_seconds=duration_seconds,
        )


def on_print_failed(printer_id: int, filename: str, reason: str = None):
    """Called by PrusaLink/Elegoo monitors when a print fails."""
    pj_id = _active_print_jobs.pop(printer_id, None) or _resolve_education_print_job(printer_id, filename)
    if pj_id:
        job_completed(
            printer_id=printer_id,
            print_job_id=pj_id,
            success=False,
            fail_reason=reason,
        )


def on_print_cancelled(printer_id: int, filename: str = None):
    """Called by PrusaLink/Elegoo monitors when a print is cancelled."""
    pj_id = _active_print_jobs.pop(printer_id, None) or _resolve_education_print_job(printer_id, filename)
    if pj_id:
        job_cancelled(
            printer_id=printer_id,
            print_job_id=pj_id,
        )


def _resolve_education_print_job(printer_id: int, filename=None) -> int | None:
    """Recover only a unique policy-linked running observation after restart."""
    policy = _education_policy()
    try:
        with get_db() as conn:
            result = policy.resolve_active_monitor_observation(conn, printer_id=printer_id) if policy else {}
        if result.get("authorized"):
            return int(result["print_job_id"])
        if result.get("education_owned"):
            log.error(
                "Education observation recovery refused for printer %s (ambiguous=%s)",
                printer_id,
                result.get("ambiguous", False),
            )
    except Exception as exc:
        log.error("Education observation recovery failed for printer %s: %s", printer_id, exc)
    # Generic monitors also reconcile a unique durable running observation.
    try:
        with get_db() as conn:
            from core.dispatch_observations import generic_observation_reserved, prepare_generic_observation
            if generic_observation_reserved(conn, printer_id=printer_id, observed_filename=filename):
                identifier=prepare_generic_observation(conn,printer_id=printer_id,observed_filename=filename)
                conn.commit()
                return identifier
            rows = conn.execute("SELECT p.id,p.filename,p.job_name FROM print_jobs p WHERE p.printer_id=? AND p.status='running' AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=p.scheduled_job_id) LIMIT 2", (printer_id,)).fetchall()
            if len(rows)!=1:return None
            if filename not in (None,'Unknown') and filename not in (rows[0][1],rows[0][2]):return None
            return rows[0][0]
    except Exception:
        log.warning("Generic terminal reconciliation unavailable")
        return None


def on_print_paused(printer_id: int):
    """Called by PrusaLink/Elegoo monitors when a print is paused."""
    from core.job_timeline import for_printer
    for_printer(printer_id, "paused")
    log.info(f"Print paused on printer {printer_id}")


def on_progress_update(printer_id: int, progress_percent: float,
                       current_layer: int = None, total_layers: int = None,
                       remaining_min: int = None):
    """Called by PrusaLink/Elegoo monitors for progress updates."""
    pj_id = _active_print_jobs.get(printer_id)
    if pj_id:
        update_job_progress(
            print_job_id=pj_id,
            progress_percent=int(progress_percent) if progress_percent is not None else None,
            remaining_minutes=remaining_min,
            current_layer=current_layer,
        )



def on_print_resumed(printer_id: int):
    from core.job_timeline import for_printer
    for_printer(printer_id, 'resumed')
