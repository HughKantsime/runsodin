"""
Job lifecycle event handlers.

Provides job_started(), job_completed(), update_job_progress() and the
monitor compatibility wrappers used by PrusaLink and Elegoo monitors.
"""

import logging
from typing import Optional

from core.db_utils import get_db
from core.event_bus import get_event_bus
from core.interfaces.event_bus import Event
from core import events as ev
from modules.notifications.printer_health import increment_care_counters
from modules.notifications.alert_dispatch import dispatch_alert, _start_bed_cooled_monitor
from modules.notifications.error_handling import record_error

log = logging.getLogger("printer_events")

# Per-printer active print_job_id tracking for monitors that don't track it themselves
_active_print_jobs = {}  # printer_id -> print_job_id


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
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO print_jobs
                    (printer_id, job_name, started_at, status, total_layers, scheduled_job_id)
                VALUES (?, ?, datetime('now'), 'running', ?, ?)""",
                (printer_id, job_name, total_layers, scheduled_job_id)
            )
            job_id = cur.lastrowid
            conn.commit()

        log.info(f"Job started on printer {printer_id}: {job_name} (print_jobs.id={job_id})")

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
        return job_id

    except Exception as e:
        log.error(f"Failed to record job start for printer {printer_id}: {e}")
        return None


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

    try:
        with get_db() as conn:
            cur = conn.cursor()

            # Update print_jobs record
            cur.execute(
                """UPDATE print_jobs SET
                    status = ?,
                    ended_at = datetime('now')
                WHERE id = ?""",
                (status, print_job_id)
            )

            # Get job details for alert
            cur.execute(
                "SELECT job_name, scheduled_job_id FROM print_jobs WHERE id = ?",
                (print_job_id,)
            )
            row = cur.fetchone()
            job_name = row[0] if row else "Unknown"
            scheduled_job_id = row[1] if row else None

            # Get printer name
            cur.execute("SELECT name, nickname FROM printers WHERE id = ?", (printer_id,))
            prow = cur.fetchone()
            printer_name = prow[1] or prow[0] if prow else f"Printer {printer_id}"

            # Update scheduled job status if linked
            if scheduled_job_id:
                cur.execute(
                    "UPDATE jobs SET status = ? WHERE id = ?",
                    (status, scheduled_job_id)
                )

            conn.commit()

        # Increment care counters if successful
        if success and duration_seconds:
            print_hours = duration_seconds / 3600.0
            increment_care_counters(printer_id, print_hours, 1)

        # Create alert
        if success:
            dispatch_alert(
                alert_type="print_complete",
                severity="success",
                title=f"Print Complete: {job_name}",
                message=f"Finished on {printer_name}",
                printer_id=printer_id,
                job_id=scheduled_job_id,
            )
        else:
            dispatch_alert(
                alert_type="print_failed",
                severity="error",
                title=f"Print Failed: {job_name}",
                message=f"Failed on {printer_name}" + (f": {fail_reason}" if fail_reason else ""),
                printer_id=printer_id,
                job_id=scheduled_job_id,
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

        bus = get_event_bus()
        bus.publish(Event(
            event_type=ev.JOB_COMPLETED if success else ev.JOB_FAILED,
            source_module="notifications",
            data={
                "printer_id": printer_id,
                "job_name": job_name,
                "status": status,
                "print_job_id": print_job_id,
                "scheduled_job_id": scheduled_job_id,
                "success": success,
                "duration_seconds": duration_seconds,
            },
        ))

        # Start bed-cooled monitoring if enabled and print was successful
        if success:
            _start_bed_cooled_monitor(printer_id, printer_name)

    except Exception as e:
        log.error(f"Failed to record job completion for printer {printer_id}: {e}")


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

            cur.execute(
                """UPDATE print_jobs SET
                    status = 'cancelled',
                    ended_at = datetime('now')
                WHERE id = ?""",
                (print_job_id,)
            )

            cur.execute(
                "SELECT job_name, scheduled_job_id FROM print_jobs WHERE id = ?",
                (print_job_id,)
            )
            row = cur.fetchone()
            job_name = row[0] if row else "Unknown"
            scheduled_job_id = row[1] if row else None

            cur.execute("SELECT name, nickname FROM printers WHERE id = ?", (printer_id,))
            prow = cur.fetchone()
            printer_name = prow[1] or prow[0] if prow else f"Printer {printer_id}"

            if scheduled_job_id:
                cur.execute(
                    "UPDATE jobs SET status = 'cancelled' WHERE id = ?",
                    (scheduled_job_id,)
                )

            conn.commit()

        dispatch_alert(
            alert_type="print_cancelled",
            severity="warning",
            title=f"Print Cancelled: {job_name}",
            message=f"Cancelled on {printer_name}",
            printer_id=printer_id,
            job_id=scheduled_job_id,
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
                "scheduled_job_id": scheduled_job_id,
                "success": False,
            },
        ))

    except Exception as e:
        log.error(f"Failed to record job cancellation for printer {printer_id}: {e}")


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
            cur.execute(
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
    pj_id = _active_print_jobs.pop(printer_id, None)
    if pj_id:
        job_completed(
            printer_id=printer_id,
            print_job_id=pj_id,
            success=True,
            duration_seconds=duration_seconds,
        )


def on_print_failed(printer_id: int, filename: str, reason: str = None):
    """Called by PrusaLink/Elegoo monitors when a print fails."""
    pj_id = _active_print_jobs.pop(printer_id, None)
    if pj_id:
        job_completed(
            printer_id=printer_id,
            print_job_id=pj_id,
            success=False,
            fail_reason=reason,
        )


def on_print_cancelled(printer_id: int, filename: str = None):
    """Called by PrusaLink/Elegoo monitors when a print is cancelled."""
    pj_id = _active_print_jobs.pop(printer_id, None)
    if pj_id:
        job_cancelled(
            printer_id=printer_id,
            print_job_id=pj_id,
        )


def on_print_paused(printer_id: int):
    """Called by PrusaLink/Elegoo monitors when a print is paused."""
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
