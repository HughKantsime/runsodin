"""
O.D.I.N. — Print Archive

Auto-captures a print archive record when a job completes.
Called from printer_events.job_completed().
"""

import logging

from db_utils import get_db

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
            conn.commit()
            log.info(f"Print archive created for job '{job_name}' on printer {printer_id}")

    except Exception as e:
        log.error(f"Failed to create print archive for print_job {print_job_id}: {e}")
