"""printer_dispatch.py — Automated job dispatch to 3D printers.

Supports:
  - Bambu Lab (FTPS implicit TLS + MQTT project_file command)
  - Moonraker/Klipper (HTTP multipart upload + REST print start)
  - PrusaLink (HTTP multipart upload + print start)

File format constraint:
  Bambu printers accept .3mf (they slice internally).
  Moonraker/PrusaLink printers require pre-sliced .gcode or .bgcode.
  Dispatching a .3mf to a non-Bambu printer returns a clear error.

AUTO_DISPATCH env var controls automatic triggering on IDLE transitions
(default: false). Manual dispatch via POST /api/jobs/{id}/dispatch always works.

WebSocket events pushed for UI progress feedback:
  job_dispatch_event {job_id, status: uploading|starting|dispatched|failed, message}
"""

import os
import logging
import time
from typing import Optional

log = logging.getLogger("odin.dispatch")

# Graceful import — ws_hub may not be running in all contexts
try:
    from ws_hub import push_event as _ws_push
except ImportError:
    def _ws_push(*a, **kw): pass


def _ws(job_id: int, status: str, message: str):
    """Push a dispatch progress event to connected WebSocket clients."""
    try:
        _ws_push("job_dispatch_event", {"job_id": job_id, "status": status, "message": message})
    except Exception:
        pass


# ──────────────────────────────────────────────
# Credential loading
# ──────────────────────────────────────────────

def _get_printer_info(printer_id: int) -> Optional[dict]:
    """Load printer credentials and api_type from DB.

    Returns a dict with keys: api_type, ip, port, and type-specific creds.
    Returns None if the printer can't be loaded.
    """
    import sqlite3
    import crypto
    from db_utils import get_db

    try:
        with get_db(row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                "SELECT api_type, api_host, api_key FROM printers WHERE id = ?",
                (printer_id,),
            ).fetchone()
    except Exception as e:
        log.error(f"[dispatch] DB error loading printer {printer_id}: {e}")
        return None

    if not row:
        log.warning(f"[dispatch] Printer {printer_id} not found")
        return None

    api_type = (row["api_type"] or "").lower()
    api_host = row["api_host"] or ""
    api_key_raw = row["api_key"] or ""

    # Parse host:port
    host, port = api_host, 80
    if ":" in api_host:
        h, p = api_host.rsplit(":", 1)
        try:
            host, port = h, int(p)
        except ValueError:
            pass

    info = {"api_type": api_type, "ip": host, "port": port}

    if api_type == "bambu":
        if not api_key_raw:
            log.warning(f"[dispatch] Bambu printer {printer_id} has no api_key")
            return None
        try:
            decrypted = crypto.decrypt(api_key_raw)
            parts = decrypted.split("|")
            if len(parts) != 2:
                log.warning(f"[dispatch] Malformed Bambu credential for printer {printer_id}")
                return None
            info["serial"] = parts[0]
            info["access_code"] = parts[1]
        except Exception as e:
            log.error(f"[dispatch] Failed to decrypt Bambu credentials: {e}")
            return None

    elif api_type == "moonraker":
        # api_key is an optional plain or Fernet-encrypted API key
        info["api_key"] = ""
        if api_key_raw:
            try:
                info["api_key"] = crypto.decrypt(api_key_raw)
            except Exception:
                info["api_key"] = api_key_raw  # plain key

    elif api_type == "prusalink":
        # api_key is Fernet "username|password" or a plain API key
        info["username"] = "maker"
        info["password"] = ""
        info["api_key"] = ""
        if api_key_raw:
            try:
                decrypted = crypto.decrypt(api_key_raw)
                if "|" in decrypted:
                    info["username"], info["password"] = decrypted.split("|", 1)
                else:
                    info["api_key"] = decrypted
            except Exception:
                info["api_key"] = api_key_raw

    return info


# ──────────────────────────────────────────────
# Job lookup
# ──────────────────────────────────────────────

def _get_next_scheduled_job(printer_id: int) -> Optional[dict]:
    """Find the next SCHEDULED job for this printer that has a stored file on disk."""
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


def _load_job(job_id: int) -> Optional[dict]:
    """Load a specific job's dispatch info from the DB."""
    import sqlite3
    from db_utils import get_db

    try:
        with get_db(row_factory=sqlite3.Row) as conn:
            row = conn.execute(
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
        return dict(row) if row else None
    except Exception as e:
        log.error(f"[dispatch] Failed to load job {job_id}: {e}")
        return None


# ──────────────────────────────────────────────
# Protocol-specific dispatch handlers
# ──────────────────────────────────────────────

def _dispatch_bambu(job_id: int, stored_path: str, remote_filename: str, creds: dict) -> tuple[bool, str]:
    """Dispatch to a Bambu printer: implicit FTPS upload + MQTT project_file."""
    from bambu_adapter import BambuPrinter

    printer = BambuPrinter(
        ip=creds["ip"],
        serial=creds["serial"],
        access_code=creds["access_code"],
        client_id=f"odin_dispatch_{creds['serial']}_{int(time.time())}",
    )

    _ws(job_id, "uploading", f"Uploading {remote_filename} to printer via FTP...")
    log.info(f"[dispatch] Bambu FTPS upload: {remote_filename}")
    if not printer.upload_file(stored_path, remote_filename):
        return False, "FTPS upload failed — check printer IP, access code, and network"

    _ws(job_id, "starting", "File uploaded, connecting MQTT to start print...")
    if not printer.connect():
        return False, "MQTT connection failed after successful file upload"

    time.sleep(1)
    log.info(f"[dispatch] Sending Bambu print command for '{remote_filename}'")
    ok = printer.start_print(remote_filename)
    printer.disconnect()

    if not ok:
        return False, "MQTT print command failed (file uploaded OK)"
    return True, "Print started"


def _dispatch_moonraker(job_id: int, stored_path: str, remote_filename: str, creds: dict) -> tuple[bool, str]:
    """Dispatch to a Moonraker printer: HTTP multipart upload + REST start."""
    from moonraker_adapter import MoonrakerPrinter

    printer = MoonrakerPrinter(
        host=creds["ip"],
        port=creds["port"],
        api_key=creds.get("api_key", ""),
    )

    _ws(job_id, "uploading", f"Uploading {remote_filename} to Moonraker...")
    log.info(f"[dispatch] Moonraker upload: {remote_filename} → {creds['ip']}")
    if not printer.upload_file(stored_path, remote_filename):
        return False, "Moonraker file upload failed — check host and API key"

    _ws(job_id, "starting", "File uploaded, sending print command...")
    log.info(f"[dispatch] Moonraker start_print: {remote_filename}")
    if not printer.start_print(remote_filename):
        return False, "Moonraker print start failed (file uploaded OK)"
    return True, "Print started"


def _dispatch_prusalink(job_id: int, stored_path: str, remote_filename: str, creds: dict) -> tuple[bool, str]:
    """Dispatch to a PrusaLink printer: multipart upload + auto-start."""
    from prusalink_adapter import PrusaLinkPrinter

    printer = PrusaLinkPrinter(
        host=creds["ip"],
        port=creds["port"],
        username=creds.get("username", "maker"),
        password=creds.get("password", ""),
        api_key=creds.get("api_key", ""),
    )

    _ws(job_id, "uploading", f"Uploading {remote_filename} to PrusaLink...")
    log.info(f"[dispatch] PrusaLink upload+start: {remote_filename} → {creds['ip']}")
    if not printer.upload_and_print(stored_path, remote_filename):
        return False, "PrusaLink upload or print start failed — check host and credentials"
    return True, "Print started"


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def dispatch_job(printer_id: int, job_id: int) -> tuple[bool, str]:
    """Dispatch a specific job to its assigned printer.

    Validates the job, loads printer credentials, selects the appropriate
    protocol handler, runs the upload+start sequence, and updates job status.

    Returns:
        (success, message) tuple.
    """
    from db_utils import get_db

    # Load job
    job = _load_job(job_id)
    if not job:
        return False, "Job not found or has no linked print file with a stored path"

    if job["printer_id"] != printer_id:
        return False, f"Job {job_id} is assigned to printer {job['printer_id']}, not {printer_id}"

    if job["status"] not in ("scheduled", "pending"):
        return False, f"Job {job_id} is in '{job['status']}' status — cannot dispatch"

    stored_path = job.get("stored_path", "")
    if not stored_path or not os.path.exists(stored_path):
        return False, f"Print file not found on disk: {stored_path or 'none'}"

    # Determine remote filename
    remote_filename = job.get("original_filename") or os.path.basename(stored_path)
    if not remote_filename.endswith((".3mf", ".gcode", ".bgcode")):
        remote_filename += ".3mf"  # assume 3mf if no extension

    # Load printer credentials
    creds = _get_printer_info(printer_id)
    if not creds:
        return False, f"Cannot retrieve credentials for printer {printer_id}"

    api_type = creds["api_type"]

    # File format validation: non-Bambu printers need .gcode
    if api_type != "bambu" and stored_path.lower().endswith(".3mf"):
        return False, (
            f"{api_type.title()} printers cannot print .3mf files — they need pre-sliced .gcode. "
            "Upload a .gcode file for this model to enable dispatch."
        )

    log.info(
        f"[dispatch] Dispatching job {job_id} ('{job['item_name']}') "
        f"to {api_type} printer {printer_id} @ {creds['ip']}"
    )
    _ws(job_id, "uploading", f"Dispatching '{job['item_name']}'...")

    # Route to appropriate handler
    if api_type == "bambu":
        success, message = _dispatch_bambu(job_id, stored_path, remote_filename, creds)
    elif api_type == "moonraker":
        success, message = _dispatch_moonraker(job_id, stored_path, remote_filename, creds)
    elif api_type == "prusalink":
        success, message = _dispatch_prusalink(job_id, stored_path, remote_filename, creds)
    else:
        return False, f"Dispatch not supported for printer type '{api_type}'"

    if not success:
        _ws(job_id, "failed", message)
        return False, message

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

    _ws(job_id, "dispatched", f"'{job['item_name']}' sent to printer successfully")
    log.info(f"[dispatch] Job {job_id} dispatched successfully ({api_type})")
    return True, "Print started"


def attempt_dispatch(printer_id: int):
    """Try to dispatch the next scheduled job to this printer.

    Called automatically when the printer transitions to idle after a print.
    No-ops unless AUTO_DISPATCH=true in environment.
    """
    auto_dispatch = os.environ.get("AUTO_DISPATCH", "false").lower() == "true"
    if not auto_dispatch:
        log.debug(f"[dispatch] AUTO_DISPATCH disabled — skipping printer {printer_id}")
        return

    job = _get_next_scheduled_job(printer_id)
    if not job:
        log.info(f"[dispatch] No queued jobs with stored files for printer {printer_id}")
        return

    log.info(f"[dispatch] Auto-dispatch: printer {printer_id} → job {job['id']} ('{job['item_name']}')")
    success, msg = dispatch_job(printer_id, job["id"])
    if success:
        log.info(f"[dispatch] Auto-dispatch succeeded: {msg}")
    else:
        log.warning(f"[dispatch] Auto-dispatch failed: {msg}")
