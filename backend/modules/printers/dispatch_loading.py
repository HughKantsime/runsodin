"""Dispatch configuration and selected payload loading; no device I/O."""
import logging
from contextlib import nullcontext
from typing import Optional
log = logging.getLogger("odin.dispatch")

def _get_printer_info(printer_id: int, *, connection=None) -> Optional[dict]:
    """Load printer credentials and api_type from DB.

    Returns a dict with keys: api_type, ip, port, and type-specific creds.
    Returns None if the printer can't be loaded.
    """
    import core.crypto as crypto
    from core.db import engine
    from sqlalchemy import text

    try:
        with (nullcontext(connection) if connection is not None else engine.connect()) as conn:
            row = conn.execute(
                text(
                    "SELECT api_type, api_host, api_key, bed_x_mm, bed_y_mm, "
                    "nozzle_diameter, machine_type, model, name, org_id, is_active, shared, "
                    "plug_type,plug_host,plug_entity_id,plug_auth_token,plug_auto_on "
                    "FROM printers WHERE id = :id"
                ),
                {"id": printer_id},
            ).fetchone()
            slot_rows = conn.execute(
                text(
                    "SELECT filament_type,slot_number FROM filament_slots "
                    "WHERE printer_id=:id AND filament_type IS NOT NULL "
                    "AND filament_type NOT IN ('empty','Unknown','OTHER')"
                ),
                {"id": printer_id},
            ).fetchall()
    except Exception as e:
        log.error("[dispatch] Printer configuration could not be loaded")
        return None

    if not row:
        log.warning(f"[dispatch] Printer {printer_id} not found")
        return None

    r = row._mapping
    # Raw textual queries preserve legacy SQLite values. Never coerce arbitrary
    # nonempty text or nonzero integers into active/sharing permission.
    flags={}
    for name in ('is_active','shared'):
        value=r[name]
        if value is None:flags[name]=False
        elif type(value) is bool:flags[name]=value
        elif type(value) is int and value in (0,1):flags[name]=bool(value)
        else:
            log.warning('Printer permission flags could not be verified')
            return None
    api_type = (r["api_type"] or "").lower()
    api_host = r["api_host"] or ""
    api_key_raw = r["api_key"] or ""

    # Parse host:port
    host, port = api_host, 80
    if ":" in api_host:
        h, p = api_host.rsplit(":", 1)
        try:
            host, port = h, int(p)
        except ValueError:
            pass

    info = {
        "api_type": api_type,
        "ip": host,
        "port": port,
        "bed_x_mm": r["bed_x_mm"],
        "bed_y_mm": r["bed_y_mm"],
        "nozzle_diameter": r["nozzle_diameter"],
        "machine_type": r["machine_type"],
        "model": r["model"],
        "name": r["name"],
        "org_id": r["org_id"],
        "is_active": flags["is_active"],
        "shared": flags["shared"],
        "active_materials": [slot[0] for slot in slot_rows],
        "filament_slots": [[slot[1],slot[0]] for slot in slot_rows],
    }

    info["plug_configuration"] = {name:r[name] for name in (
        "plug_type","plug_host","plug_entity_id","plug_auth_token","plug_auto_on")}

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
            log.error("[dispatch] Printer credentials could not be decoded")
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



def _get_next_scheduled_job(printer_id: int, *, generic_only: bool = False) -> Optional[dict]:
    """Find the next SCHEDULED job for this printer that has a stored file on disk."""
    from core.db import engine
    from sqlalchemy import text
    from core.dispatch_payload_selection import SELECTED_PRINT_FILE_JOIN_SQL

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: the SELECTED_PRINT_FILE_JOIN_SQL module constant; values are bound
                    f"""
                SELECT j.id, j.item_name, j.printer_id, j.priority, j.queue_position, j.created_at,
                       s.id AS education_submission_id, pf.stored_path, pf.original_filename
                FROM jobs j
                LEFT JOIN education_submissions s ON s.job_id=j.id
                JOIN models m ON j.model_id=m.id
                JOIN print_files pf ON {SELECTED_PRINT_FILE_JOIN_SQL}
                WHERE j.printer_id = :pid
                  AND j.status = 'scheduled'
                  AND pf.stored_path IS NOT NULL
                  AND pf.stored_path != ''
                ORDER BY j.priority ASC, (j.queue_position IS NULL) ASC, j.queue_position ASC,
                         j.created_at ASC, j.id ASC
                """),
                {"pid": printer_id},
            ).fetchall()
            from modules.jobs.services import education_order, ordered_waiting
            candidates = [dict(row._mapping) for row in rows]
            candidates = education_order(candidates,
                {row['id'] for row in candidates if row['education_submission_id'] is not None},
                lambda row, key: row[key])
            waiting = ordered_waiting(conn, printer_id)
            head = waiting[0]['id'] if waiting and not waiting[0]['hold'] else None
            # A blocked/pending head stalls Education only; generic candidates remain.
            return next((row for row in candidates
                if row['education_submission_id'] is None or (not generic_only and row['id'] == head)), None)
    except Exception as e:
        log.error(f"[dispatch] Job lookup failed for printer {printer_id}: {e}")
        return None



def _load_job(job_id: int, *, connection=None) -> Optional[dict]:
    """Load a specific job's dispatch info from the DB."""
    from core.db import engine
    from sqlalchemy import text
    from core.dispatch_payload_selection import SELECTED_PRINT_FILE_JOIN_SQL

    try:
        with (nullcontext(connection) if connection is not None else engine.connect()) as conn:
            row = conn.execute(
                text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: the SELECTED_PRINT_FILE_JOIN_SQL module constant; values are bound
                    f"""
                SELECT j.id, j.model_id, j.item_name, j.status, j.printer_id, j.hold, j.order_item_id,
                       j.charged_to_user_id, j.charged_to_org_id, j.submitted_by,
                       j.print_file_id, j.payload_sha256, j.plate_index, j.ams_mapping,
                       pf.id AS resolved_print_file_id, pf.org_id AS print_file_org_id,
                       m.org_id AS model_org_id, m.print_file_id AS model_print_file_id, pf.filaments_json,
                       pf.stored_path, pf.original_filename, pf.file_hash,
                       pf.bed_x_mm, pf.bed_y_mm, pf.compatible_api_types,
                       pf.compatibility_facts_json,
                       s.id AS education_submission_id,
                       s.org_id AS education_org_id,
                       s.cost_center_id AS education_cost_center_id,
                       s.status AS education_status,
                       s.lifecycle_revision AS education_lifecycle_revision,
                       s.approved_printer_id AS education_approved_printer_id
                FROM jobs j
                LEFT JOIN education_submissions s ON s.job_id = j.id
                JOIN models m ON j.model_id = m.id
                JOIN print_files pf ON {SELECTED_PRINT_FILE_JOIN_SQL}
                WHERE j.id = :jid
                """),
                {"jid": job_id},
            ).fetchone()
            if row:
                from core.print_file_binding import target_printer
                job = dict(row._mapping)
                job["upload_target_printer_id"] = target_printer(conn, job["resolved_print_file_id"])
                return job
        return None
    except Exception as e:
        log.error("[dispatch] Job configuration could not be loaded")
        return None
