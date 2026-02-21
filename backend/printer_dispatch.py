"""printer_dispatch.py — Automated job dispatch to Bambu printers.

Handles the full dispatch cycle for a scheduled job:
  1. Find the next SCHEDULED job for a printer that has a stored .3mf file
  2. Decrypt printer credentials (Fernet serial|access_code)
  3. Upload the .3mf file via implicit FTPS (port 990)
  4. Issue an MQTT project_file command to start the print
  5. Update job status to 'printing'

AUTO_DISPATCH environment variable controls automatic triggering on IDLE
transitions (default: false, intentional safety default to prevent bed crashes
from unwanted auto-starts).

Manual dispatch is always available via POST /api/jobs/{job_id}/dispatch.
"""

import os
import logging
import time
from typing import Optional

log = logging.getLogger("odin.dispatch")


def _get_printer_creds(printer_id: int) -> Optional[dict]:
    """Look up and decrypt Bambu printer credentials from the DB.

    Returns dict with keys {ip, serial, access_code}, or None on failure.
    """
    import sqlite3
    import crypto
    from db_utils import get_db

    try:
        with get_db(row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                "SELECT api_host, api_key FROM printers"
                " WHERE id = ? AND api_host IS NOT NULL AND api_key != ''",
                (printer_id,),
            ).fetchone()

        if not row:
            log.warning(f"[dispatch] No credentials found for printer {printer_id}")
            return None

        decrypted = crypto.decrypt(row["api_key"])
        parts = decrypted.split("|")
        if len(parts) != 2:
            log.warning(f"[dispatch] Malformed credential for printer {printer_id}")
            return None

        return {"ip": row["api_host"], "serial": parts[0], "access_code": parts[1]}
    except Exception as e:
        log.error(f"[dispatch] Failed to get creds for printer {printer_id}: {e}")
        return None


def _get_next_scheduled_job(printer_id: int) -> Optional[dict]:
    """Find the next SCHEDULED job for this printer that has a stored .3mf file on disk.

    Returns dict with job fields, or None if nothing is queued.
    """
    import sqlite3
    from db_utils import get_db

    try:
        with get_db(row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                """
                SELECT j.id, j.item_name, pf.stored_path, pf.original_filename
                FROM jobs j
                JOIN models m ON j.model_id = m.id
                JOIN print_files pf ON pf.model_id = m.id
                WHERE j.printer_id = ?
                  AND j.status = 'scheduled'
                  AND pf.stored_path IS NOT NULL
                  AND pf.stored_path != ''
                ORDER BY j.queue_position ASC, j.priority ASC, j.created_at ASC
                LIMIT 1
                """,
                (printer_id,),
            ).fetchone()

        return dict(row) if row else None
    except Exception as e:
        log.error(f"[dispatch] Job lookup failed for printer {printer_id}: {e}")
        return None


def dispatch_job(printer_id: int, job_id: int) -> tuple[bool, str]:
    """Dispatch a specific job to a Bambu printer.

    Uploads the .3mf file via FTPS then sends the MQTT print command.
    Updates job status to 'printing' on success.

    Returns:
        (success, message) tuple.
    """
    import sqlite3
    from db_utils import get_db

    # Load job + file info
    try:
        with get_db(row_factory=sqlite3.Row) as conn:
            job_row = conn.execute(
                """
                SELECT j.id, j.item_name, j.status, j.printer_id,
                       pf.stored_path, pf.original_filename
                FROM jobs j
                JOIN models m ON j.model_id = m.id
                JOIN print_files pf ON pf.model_id = m.id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
    except Exception as e:
        return False, f"DB error: {e}"

    if not job_row:
        return False, "Job not found or has no linked print file with a stored path"

    job = dict(job_row)

    if job["printer_id"] != printer_id:
        return False, f"Job {job_id} is assigned to printer {job['printer_id']}, not {printer_id}"

    if job["status"] not in ("scheduled", "pending"):
        return False, f"Job {job_id} is in '{job['status']}' status — cannot dispatch"

    stored_path = job.get("stored_path", "")
    if not stored_path or not os.path.exists(stored_path):
        return False, f"Print file not found on disk: {stored_path or 'none'}"

    # Resolve remote filename
    remote_filename = job.get("original_filename") or os.path.basename(stored_path)
    if not remote_filename.endswith(".3mf"):
        remote_filename += ".3mf"

    # Get credentials
    creds = _get_printer_creds(printer_id)
    if not creds:
        return False, f"Cannot retrieve credentials for printer {printer_id}"

    log.info(
        f"[dispatch] Dispatching job {job_id} ('{job['item_name']}') "
        f"to printer {printer_id} @ {creds['ip']}"
    )
    log.info(f"[dispatch] File: {stored_path} → {remote_filename}")

    # Upload via FTPS (does NOT need MQTT connection)
    from bambu_adapter import BambuPrinter

    printer = BambuPrinter(
        ip=creds["ip"],
        serial=creds["serial"],
        access_code=creds["access_code"],
        client_id=f"odin_dispatch_{printer_id}_{int(time.time())}",
    )

    log.info(f"[dispatch] Uploading via FTPS...")
    upload_ok = printer.upload_file(stored_path, remote_filename)
    if not upload_ok:
        return False, "FTPS upload failed — check printer IP, access code, and network"

    log.info(f"[dispatch] Upload complete, connecting MQTT...")

    # Connect MQTT for the print command
    if not printer.connect():
        return False, "MQTT connection failed after successful file upload"

    # Brief pause so the printer registers the new file
    time.sleep(1)

    log.info(f"[dispatch] Sending print command for '{remote_filename}'...")
    print_ok = printer.start_print(remote_filename)
    printer.disconnect()

    if not print_ok:
        return False, "MQTT print command failed (file uploaded OK — printer may have started anyway)"

    # Mark job as printing
    try:
        from datetime import datetime, timezone
        with get_db() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'printing',"
                " actual_start = COALESCE(actual_start, ?) WHERE id = ?",
                (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), job_id),
            )
            conn.commit()
    except Exception as e:
        log.warning(f"[dispatch] DB status update failed (print already started): {e}")

    log.info(f"[dispatch] Job {job_id} dispatched successfully")
    return True, "Print started"


def attempt_dispatch(printer_id: int):
    """Try to dispatch the next scheduled job to this printer.

    Called automatically when the printer transitions to IDLE after FINISH/FAILED.
    No-ops if the AUTO_DISPATCH environment variable is not set to 'true'.
    """
    auto_dispatch = os.environ.get("AUTO_DISPATCH", "false").lower() == "true"
    if not auto_dispatch:
        log.debug(
            f"[dispatch] AUTO_DISPATCH disabled — skipping auto-dispatch for printer {printer_id}"
        )
        return

    job = _get_next_scheduled_job(printer_id)
    if not job:
        log.info(f"[dispatch] No queued jobs with stored files for printer {printer_id}")
        return

    log.info(
        f"[dispatch] Auto-dispatch triggered for printer {printer_id}: job {job['id']} ('{job['item_name']}')"
    )
    success, msg = dispatch_job(printer_id, job["id"])
    if success:
        log.info(f"[dispatch] Auto-dispatch succeeded: {msg}")
    else:
        log.warning(f"[dispatch] Auto-dispatch failed: {msg}")
