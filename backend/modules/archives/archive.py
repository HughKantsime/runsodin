"""
O.D.I.N. — Print Archive

Auto-captures a print archive record when a job completes.
Called from printer_events.job_completed().
"""

import logging

from core.db_utils import get_db

log = logging.getLogger("odin.archive")


def create_print_archive(print_job_id: int, printer_id: int, success: bool):
    """Create a print_archives row from a completed print_jobs record.

    Args:
        print_job_id: Row ID in print_jobs table.
        printer_id: Printer that ran the job.
        success: True if completed, False if failed/cancelled.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()

            # Fetch the print_jobs row
            cur.execute(
                "SELECT job_name, started_at, ended_at, scheduled_job_id, "
                "filament_slots, status FROM print_jobs WHERE id = ?",
                (print_job_id,),
            )
            pj = cur.fetchone()
            if not pj:
                log.debug(f"Archive skip: print_job {print_job_id} not found")
                return

            job_name = pj[0] or "Unknown"
            started_at = pj[1]
            ended_at = pj[2]
            scheduled_job_id = pj[3]
            status = pj[5] or ("completed" if success else "failed")

            # Compute actual duration
            duration_seconds = None
            if started_at and ended_at:
                try:
                    from datetime import datetime
                    fmt = "%Y-%m-%d %H:%M:%S"
                    # Handle both ISO and sqlite datetime formats
                    sa = started_at.replace("T", " ").split(".")[0]
                    ea = ended_at.replace("T", " ").split(".")[0]
                    dt_start = datetime.strptime(sa, fmt)
                    dt_end = datetime.strptime(ea, fmt)
                    duration_seconds = int((dt_end - dt_start).total_seconds())
                except Exception:
                    pass

            # Try to find filament used from the scheduled job's print_file
            filament_used = None
            thumbnail_b64 = None
            file_path = None
            user_id = None
            cost_estimate = None

            if scheduled_job_id:
                # Get user_id from the scheduled job (submitted_by)
                cur.execute(
                    "SELECT submitted_by FROM jobs WHERE id = ?",
                    (scheduled_job_id,),
                )
                job_row = cur.fetchone()
                if job_row:
                    user_id = job_row[0]

                # Get print file info (thumbnail, weight, path)
                cur.execute(
                    "SELECT thumbnail_b64, stored_path, filament_weight_grams "
                    "FROM print_files WHERE job_id = ?",
                    (scheduled_job_id,),
                )
                pf = cur.fetchone()
                if pf:
                    thumbnail_b64 = pf[0]
                    file_path = pf[1]
                    filament_used = pf[2]

            # Insert archive row
            cur.execute(
                """INSERT INTO print_archives
                   (job_id, print_job_id, printer_id, user_id, print_name,
                    status, started_at, completed_at, actual_duration_seconds,
                    filament_used_grams, cost_estimate, thumbnail_b64, file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scheduled_job_id,
                    print_job_id,
                    printer_id,
                    user_id,
                    job_name,
                    status,
                    started_at,
                    ended_at,
                    duration_seconds,
                    filament_used,
                    cost_estimate,
                    thumbnail_b64,
                    file_path,
                ),
            )
            archive_id = cur.lastrowid
            conn.commit()
            log.info(f"Print archive created for job '{job_name}' on printer {printer_id}")

            # Auto-deduct filament consumption from spool remaining_weight
            if filament_used and filament_used > 0 and success:
                _deduct_filament_consumption(conn, printer_id, filament_used, archive_id)

    except Exception as e:
        log.error(f"Failed to create print archive for print_job {print_job_id}: {e}")


def _deduct_filament_consumption(conn, printer_id: int, grams_used: float, archive_id: int):
    """Deduct filament from the primary spool assigned to this printer.

    Finds the active spool via filament_slots -> assigned_spool_id,
    deducts grams from remaining_weight_g, and stores consumption
    breakdown in the archive's consumption_json.
    """
    try:
        cur = conn.cursor()
        consumption = []

        # Find spools assigned to this printer's filament slots
        cur.execute("""
            SELECT fs.slot_number, fs.assigned_spool_id, s.id as spool_id,
                   s.remaining_weight_g, s.spoolman_spool_id
            FROM filament_slots fs
            LEFT JOIN spools s ON s.id = fs.assigned_spool_id
            WHERE fs.printer_id = ? AND fs.assigned_spool_id IS NOT NULL
            ORDER BY fs.slot_number
        """, (printer_id,))
        slots = cur.fetchall()

        if not slots:
            return

        # Simple approach: deduct from the first assigned spool
        # (multi-color deduction would require per-slot usage from slicer metadata)
        spool_row = slots[0]
        spool_id = spool_row[2]
        remaining = spool_row[3]
        spoolman_id = spool_row[4]

        if spool_id and remaining is not None:
            new_remaining = max(0, remaining - grams_used)
            cur.execute(
                "UPDATE spools SET remaining_weight_g = ? WHERE id = ?",
                (new_remaining, spool_id),
            )
            consumption.append({"spool_id": spool_id, "grams_used": round(grams_used, 1)})

            # Sync to Spoolman if configured
            if spoolman_id:
                try:
                    _sync_spoolman_consumption(spoolman_id, grams_used)
                except Exception as e:
                    log.debug(f"Spoolman consumption sync failed: {e}")

        # Store consumption breakdown in archive
        if consumption:
            import json
            cur.execute(
                "UPDATE print_archives SET consumption_json = ? WHERE id = ?",
                (json.dumps(consumption), archive_id),
            )
            conn.commit()
            log.debug(f"Deducted {grams_used:.1f}g from spool {spool_id} for archive {archive_id}")

    except Exception as e:
        log.warning(f"Filament consumption deduction failed for printer {printer_id}: {e}")


def _sync_spoolman_consumption(spoolman_spool_id: int, grams_used: float):
    """Sync filament consumption to Spoolman via its API."""
    import os
    import urllib.request
    import json

    base_url = os.environ.get("SPOOLMAN_URL", "").rstrip("/")
    if not base_url:
        return

    url = f"{base_url}/api/v1/spool/{spoolman_spool_id}/use"
    data = json.dumps({"use_weight": grams_used}).encode()
    req = urllib.request.Request(url, data=data, method="PUT",
                                headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


# ---------------------------------------------------------------------------
# Event bus integration
# ---------------------------------------------------------------------------

def _on_job_completed_event(event) -> None:
    """Auto-capture a print archive when a job finishes (success or failure)."""
    data = event.data
    print_job_id = data.get("print_job_id")
    printer_id = data.get("printer_id")
    success = data.get("success", True)
    if print_job_id is not None and printer_id is not None:
        try:
            create_print_archive(print_job_id, printer_id, success)
        except Exception as e:
            log.warning(f"Print archive capture failed via event bus: {e}")


def register_subscribers(bus) -> None:
    """
    Register archive event handlers on the event bus.

    Called from modules/archives/__init__.py register_subscribers().
    """
    from core import events as ev

    bus.subscribe(ev.JOB_COMPLETED, _on_job_completed_event)
    bus.subscribe(ev.JOB_FAILED, _on_job_completed_event)
    log.debug("archive subscribed to event bus")
