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
import hashlib
import hmac
import json
import logging
import time
from contextlib import nullcontext
from typing import Optional

log = logging.getLogger("odin.dispatch")


class DispatchOutcomeUncertain(RuntimeError):
    """A print-capable operation was entered without reliable confirmation."""


def _submit_print_command(operation, *args, **kwargs):
    """Do not turn a lost acknowledgment into permission to send again."""
    try:
        accepted = operation(*args, **kwargs)
    except Exception as exc:
        raise DispatchOutcomeUncertain("Printer command outcome is unknown") from exc
    if not accepted:
        raise DispatchOutcomeUncertain("Printer command outcome is unknown")
    return True

# Graceful import — ws_hub may not be running in all contexts
try:
    from core.ws_hub import push_event as _ws_push
except ImportError:
    def _ws_push(*a, **kw): pass


def _ws(job_id: int, status: str, message: str):
    """Push a dispatch progress event to connected WebSocket clients."""
    try:
        _ws_push("job_dispatch_event", {"job_id": job_id, "status": status, "message": message})
    except Exception as e:
        log.debug(f"WebSocket push failed: {e}")


# ──────────────────────────────────────────────
# Credential loading
# ──────────────────────────────────────────────

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


# ──────────────────────────────────────────────
# Job lookup
# ──────────────────────────────────────────────

def _get_next_scheduled_job(printer_id: int) -> Optional[dict]:
    """Find the next SCHEDULED job for this printer that has a stored file on disk."""
    from core.db import engine
    from sqlalchemy import text
    from core.dispatch_payload_selection import SELECTED_PRINT_FILE_JOIN_SQL

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(f"""
                SELECT j.id, j.item_name, pf.stored_path, pf.original_filename
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
                LIMIT 1
                """),
                {"pid": printer_id},
            ).fetchone()
        return dict(row._mapping) if row else None
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
                text(f"""
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
        return dict(row._mapping) if row else None
    except Exception as e:
        log.error("[dispatch] Job configuration could not be loaded")
        return None


# ──────────────────────────────────────────────
# Protocol-specific dispatch handlers
# ──────────────────────────────────────────────

def _dispatch_bambu(job_id: int, stored_path: str, remote_filename: str, creds: dict, *, before_start=None, **print_options) -> tuple[bool, str]:
    """Dispatch to a Bambu printer: implicit FTPS upload + MQTT project_file."""
    from modules.printers.telemetry.feature_flag import is_v2_enabled

    if is_v2_enabled():
        return _dispatch_bambu_v2(job_id, stored_path, remote_filename, creds, before_start=before_start, **print_options)
    return _dispatch_bambu_legacy(job_id, stored_path, remote_filename, creds, before_start=before_start, **print_options)


def _mapping_gate_available(print_options, before_start):
    return (before_start is None and print_options.get('ams_mapping') is None) or callable(before_start)


def _post_upload_gate(before_start):
    if before_start is None:
        return True
    try:
        return before_start() is True
    except Exception:
        return False


def _dispatch_bambu_v2(job_id: int, stored_path: str, remote_filename: str, creds: dict, *, before_start=None, **print_options) -> tuple[bool, str]:
    """V2 dispatch: ftp_upload helper + BambuCommandAdapter.start_print via session."""
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.broker_policy import resolve_bambu_broker_config
    from modules.printers.telemetry.bambu.ftp_upload import upload_file
    from modules.printers.telemetry.bambu.session import run_command

    if not _mapping_gate_available(print_options, before_start):
        return False, "Explicit AMS selection requires a post-upload check; no command was sent"
    _ws(job_id, "uploading", f"Uploading {remote_filename} to printer via FTP...")
    log.info(f"[dispatch] Bambu V2 FTPS upload: {remote_filename}")
    if not upload_file(creds["ip"], creds["access_code"], stored_path, remote_filename):
        return False, "FTPS upload failed — check printer IP, access code, and network"

    _ws(job_id, "starting", "File uploaded, connecting MQTT to start print...")
    endpoint = resolve_bambu_broker_config(
        creds["ip"], printer_id=f"dispatch-{creds['serial']}",
    )
    config = BambuAdapterConfig(
        printer_id=f"dispatch-{creds['serial']}",
        serial=creds["serial"],
        host=endpoint.host,
        port=endpoint.port,
        use_tls=endpoint.use_tls,
        access_code=creds["access_code"],
    )
    if not _post_upload_gate(before_start):
        return False, "Post-upload printer and AMS checks refused print start; no print command was sent"
    log.info(f"[dispatch] Sending Bambu V2 start_print for '{remote_filename}'")
    ok = _submit_print_command(run_command, config, "start_print", remote_filename, **print_options)
    if not ok:
        return False, "MQTT print command failed (file uploaded OK)"
    return True, "Print started"


def _dispatch_bambu_legacy(job_id: int, stored_path: str, remote_filename: str, creds: dict, *, before_start=None, **print_options) -> tuple[bool, str]:
    """Legacy transport with an explicit post-upload gate for mapped starts."""
    from modules.printers.adapters.bambu import BambuPrinter

    if not _mapping_gate_available(print_options, before_start):
        return False, "Explicit AMS selection requires a post-upload check; no command was sent"
    printer = BambuPrinter(
        ip=creds["ip"],
        serial=creds["serial"],
        access_code=creds["access_code"],
        client_id=f"odin_dispatch_{creds['serial']}_{int(time.time())}",
    )

    try:
        _ws(job_id, "uploading", f"Uploading {remote_filename} to printer via FTP...")
        log.info("[dispatch] Bambu FTPS upload for job %s", job_id)
        if not printer.upload_file(stored_path, remote_filename):
            return False, "FTPS upload failed; check printer settings and network"

        _ws(job_id, "starting", "File uploaded, connecting MQTT to start print...")
        if not printer.connect():
            return False, "MQTT connection failed after successful file upload"

        time.sleep(1)
        if not _post_upload_gate(before_start):
            return False, "Post-upload printer and AMS checks refused print start; no print command was sent"
        log.info("[dispatch] Sending Bambu print command for job %s", job_id)
        ok = _submit_print_command(printer.start_print, remote_filename, **print_options)
        if not ok:
            return False, "MQTT print command failed (file uploaded OK)"
        return True, "Print started"
    finally:
        # Includes failed/partial connection and upload paths, not just commands.
        # A cleanup exception reaches dispatch orchestration as uncertainty.
        printer.disconnect()


def _dispatch_moonraker(job_id: int, stored_path: str, remote_filename: str, creds: dict) -> tuple[bool, str]:
    """Dispatch to a Moonraker printer: HTTP multipart upload + REST start."""
    from modules.printers.adapters.moonraker import MoonrakerPrinter

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
    if not _submit_print_command(printer.start_print, remote_filename):
        return False, "Moonraker print start failed (file uploaded OK)"
    return True, "Print started"


def _dispatch_prusalink(job_id: int, stored_path: str, remote_filename: str, creds: dict) -> tuple[bool, str]:
    """Dispatch to a PrusaLink printer: multipart upload + auto-start."""
    from modules.printers.adapters.prusalink import PrusaLinkPrinter

    printer = PrusaLinkPrinter(
        host=creds["ip"],
        port=creds["port"],
        username=creds.get("username", "maker"),
        password=creds.get("password", ""),
        api_key=creds.get("api_key", ""),
    )

    _ws(job_id, "uploading", f"Uploading {remote_filename} to PrusaLink...")
    log.info(f"[dispatch] PrusaLink upload+start: {remote_filename} → {creds['ip']}")
    if not _submit_print_command(printer.upload_and_print, stored_path, remote_filename):
        return False, "PrusaLink upload or print start failed — check host and credentials"
    return True, "Print started"


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def _reconcile_education_denial(
    provider,
    context: dict,
    job_id: int,
    expected_revision: int,
    reason: str,
) -> None:
    from core.db import engine
    from sqlalchemy.orm import Session

    if not provider or not context:
        return
    try:
        with Session(engine) as db:
            provider.reconcile_dispatch_denial(
                db,
                submission_id=int(context["id"]),
                job_id=job_id,
                expected_revision=expected_revision,
                reason=reason,
            )
    except Exception as exc:
        log.error("[dispatch] Education denial reconciliation failed: %s", exc)


def _authorize_education_hardware_action(job: dict, creds: dict) -> tuple[bool, str] | None:
    """Final policy and compatibility check for Education-owned jobs."""
    if job.get("education_submission_id") is None:
        from core.db import engine
        from core.model_preview_access import is_education_owned_file
        from sqlalchemy import text
        identifiers = {
            job.get("resolved_print_file_id"),
            job.get("model_print_file_id"),
            job.get("print_file_id"),
        } - {None}
        with engine.connect() as conn:
            for identifier in identifiers:
                path = conn.execute(
                    text("SELECT stored_path FROM print_files WHERE id=:id"),
                    {"id": identifier},
                ).scalar_one_or_none()
                if is_education_owned_file(conn, identifier, path):
                    return False, "Education file requires its approval workflow; no hardware command was sent"
        if job.get("model_id") is not None:
            from core.db import engine
            from core.registry import registry
            from sqlalchemy.orm import Session
            provider = registry.get_provider("EducationPolicyProvider")
            if provider is None:
                return False, "Education dispatch policy is unavailable; no hardware command was sent"
            try:
                with Session(engine) as db:
                    provider.assert_generic_model_reference_allowed(db, int(job["model_id"]))
            except Exception:
                return False, "Education resource requires its submission and approval workflow; no hardware command was sent"
        return None

    from core.db import engine
    from core.registry import registry
    from modules.printers.education_compatibility import evaluate_education_compatibility
    from sqlalchemy.orm import Session

    provider = registry.get_provider("EducationPolicyProvider")
    if provider is None:
        return False, "Education dispatch policy is unavailable; no hardware command was sent"

    expected_revision = job.get("education_lifecycle_revision")
    if expected_revision is None:
        return False, "Education lifecycle revision is missing; no hardware command was sent"

    with Session(engine) as db:
        context = provider.authorize_dispatch(
            db,
            job_id=int(job["id"]),
            printer_id=int(job["printer_id"]),
            expected_revision=int(expected_revision),
        )

    if not context or not context.get("authorized"):
        if context:
            _reconcile_education_denial(
                provider,
                context,
                int(job["id"]),
                int(expected_revision),
                "current approval or entitlement is no longer valid",
            )
        return False, "Education approval or printer entitlement changed; job returned to submitted"

    try:
        file_facts = json.loads(job.get("compatibility_facts_json") or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        file_facts = {}
    result = evaluate_education_compatibility(file_facts, creds)
    if not result["compatible"]:
        codes = sorted({reason["code"] for reason in result["reasons"]})
        _reconcile_education_denial(
            provider,
            context,
            int(job["id"]),
            int(expected_revision),
            "compatibility drift: " + ",".join(codes),
        )
        return False, (
            "Education compatibility check failed ("
            + ", ".join(codes)
            + "); job returned to submitted"
        )
    return True, result["engine_version"]


def _reserve_education_hardware_action(job: dict, extension: str):
    """Commit the Education correlation claim before any adapter call."""
    if job.get("education_submission_id") is None:
        return None, None

    from core.db import engine
    from core.registry import registry
    from sqlalchemy.orm import Session

    provider = registry.get_provider("EducationPolicyProvider")
    if provider is None:
        return None, "Education dispatch policy is unavailable; no hardware command was sent"
    try:
        with Session(engine) as db:
            reservation = provider.reserve_dispatch(
                db,
                job_id=int(job["id"]),
                printer_id=int(job["printer_id"]),
                expected_revision=int(job["education_lifecycle_revision"]),
                extension=extension,
            )
            if reservation:
                db.commit()
    except Exception as exc:
        log.error("[dispatch] Education reservation failed: %s", exc)
        return None, "Education dispatch reservation failed; no hardware command was sent"
    if not reservation:
        return None, "Education authority changed or this printer already has an active print claim"
    return (provider, reservation), None


def _cancel_education_reservation(provider, reservation: dict) -> bool:
    from core.db import engine
    from sqlalchemy.orm import Session

    try:
        with Session(engine) as db:
            cancelled = provider.cancel_dispatch_reservation(
                db,
                claim_id=reservation["claim_id"],
                submission_id=int(reservation["submission_id"]),
                job_id=int(reservation["job_id"]),
                printer_id=int(reservation["printer_id"]),
                expected_revision=int(reservation["authority_revision"]),
            )
            if cancelled:
                db.commit()
            return bool(cancelled)
    except Exception as exc:
        log.error("[dispatch] Education reservation cancellation failed: %s", exc)
        return False


def _confirm_education_dispatch(provider, reservation: dict) -> dict:
    from core.db import engine
    from sqlalchemy.orm import Session

    try:
        with Session(engine) as db:
            result = provider.confirm_dispatch_started(
                db,
                claim_id=reservation["claim_id"],
                submission_id=int(reservation["submission_id"]),
                job_id=int(reservation["job_id"]),
                printer_id=int(reservation["printer_id"]),
                expected_revision=int(reservation["authority_revision"]),
            )
            if result.get("transitioned"):
                db.commit()
            return result
    except Exception as exc:
        log.error("[dispatch] Education physical start confirmation failed: %s", exc)
        return {"education_owned": True, "authorized": False, "transitioned": False}

def _notify_start_outcome(job: dict, reason: str):
    """Record a safe outcome without changing or repeating physical dispatch."""
    creator = job.get("charged_to_user_id") or job.get("submitted_by")
    if creator is None:
        return
    messages = {
        "adapter_rejected": "The printer did not confirm the requested print start. Check diagnostics before retrying.",
        "adapter_unknown": "Print start outcome is unknown. Explicit reconciliation is required before retrying.",
        "confirmation_failed": "The printer accepted the print, but its recorded state requires reconciliation before retrying.",
    }
    try:
        from core.db import engine
        from core.base import AlertType, AlertSeverity
        from sqlalchemy.orm import Session
        from sqlalchemy import text
        from modules.notifications.alert_dispatcher import lock_queue_alert, stage_alert, deliver_staged_alert
        with Session(engine) as db:
            if not lock_queue_alert(db, job["id"], AlertType.QUEUE_FAILED_START):
                return
            row = db.execute(text("SELECT charged_to_user_id,submitted_by,charged_to_org_id,printer_id "
                "FROM jobs WHERE id=:id"), {"id": job["id"]}).mappings().first()
            if (not row or (row["charged_to_user_id"] or row["submitted_by"]) != creator
                    or row["charged_to_org_id"] != job.get("charged_to_org_id")
                    or row["printer_id"] != job.get("printer_id")):
                return
            plan = stage_alert(db, AlertType.QUEUE_FAILED_START, AlertSeverity.WARNING,
                "Print start needs attention", messages[reason], job_id=job["id"],
                printer_id=job["printer_id"], metadata={"reason": reason}, target_user_ids=[creator])
            db.commit()
            deliver_staged_alert(db, plan)
    except Exception:
        log.warning("Dispatch outcome notification failed; physical result preserved")


def _validated_print_options(job,creds,stored_path,payload_sha256):
    printer_id=job['printer_id']
    api_type=creds['api_type']
    print_options = {}
    if job.get("print_file_id") is not None:
        # Revalidate exact payload immediately before any transport operation.
        from core.print_payload import validate_plate, validate_mapping
        from fastapi import HTTPException
        expected = str(job.get("payload_sha256") or "").lower()
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            return None, "Archived payload hash is missing or invalid; no command was sent"
        if not hmac.compare_digest(expected,payload_sha256):
            return None, "Archived payload changed; no command was sent"
        try:
            plate = validate_plate(stored_path, job.get("plate_index"), api_type)
        except HTTPException as exc:
            return None, str(exc.detail)
        if api_type == "bambu":
            print_options["plate_num"] = plate
            mapping = job.get("ams_mapping")
            if isinstance(mapping, str):
                try:
                    mapping = json.loads(mapping)
                except ValueError:
                    return None, "Stored AMS mapping is invalid; no command was sent"
            if mapping is not None:
                # Bambu tray index 0-15 (four-tray units 0-3). AMS HT (tray
                # id 128+) is not a dispatch target; see ams_identity.
                if (not isinstance(mapping, list) or not mapping or len(mapping) > 16
                        or any(type(slot) is not int or not 0 <= slot < 16 for slot in mapping)
                        or len(set(mapping)) != len(mapping)):
                    return None, "Stored AMS mapping is invalid; no command was sent"
                from core.db import engine
                from sqlalchemy import text
                with engine.connect() as conn:
                    slots = {row[0]: row[1] for row in conn.execute(
                        text("SELECT slot_number,filament_type FROM filament_slots WHERE printer_id=:id"),
                        {"id": printer_id}).fetchall()}
                try:
                    validate_mapping([{"source_slot": i, "target_slot": slot + 1}
                                      for i, slot in enumerate(mapping)],
                                     job.get("filaments_json"), slots, api_type)
                except HTTPException as exc:
                    return None, str(exc.detail)
                # Finish the slot transaction before bounded source inspection.
                # Predictably unsupported source shapes must not commit admission.
                from modules.printers.dispatch_ams_gate import preflight_ams_source
                try:
                    preflight_ams_source(job, mapping=mapping, staged_path=stored_path,
                                         payload_sha256=payload_sha256)
                except (ValueError, OSError):
                    return None, "Explicit AMS source or plate is unsupported; no command was sent"
                print_options["ams_mapping"] = mapping
        elif job.get("ams_mapping") is not None:
            return None, "AMS mapping requires a Bambu printer; no command was sent"

    return print_options,None


def dispatch_job(printer_id: int, job_id: int) -> tuple[bool, str]:
    """Dispatch a specific job to its assigned printer.

    Validates the job, loads printer credentials, selects the appropriate
    protocol handler, runs the upload+start sequence, and updates job status.

    Returns:
        (success, message) tuple.
    """
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

    from modules.printers.dispatch_payload_snapshot import hash_regular_payload,PayloadUnavailable
    try:payload_sha256=hash_regular_payload(stored_path)
    except PayloadUnavailable as exc:return False,str(exc)

    # Determine remote filename
    remote_filename = job.get("original_filename") or os.path.basename(stored_path)
    if not remote_filename.endswith((".3mf", ".gcode", ".bgcode")):
        remote_filename += ".3mf"  # assume 3mf if no extension

    # Load printer credentials
    creds = _get_printer_info(printer_id)
    if not creds:
        return False, f"Cannot retrieve credentials for printer {printer_id}"

    from core.dispatch_identity import ownership_refusal
    ownership_error=ownership_refusal(job,creds)
    if ownership_error:return False,ownership_error
    if creds.get('is_active') is not True or job.get('hold') not in (None,False,0):
        return False,'Printer is inactive or the job is held; no command was sent'

    api_type = creds["api_type"]

    if api_type not in {"bambu", "moonraker", "prusalink"}:
        return False, f"Dispatch not supported for printer type '{api_type}'"

    print_options,options_error=_validated_print_options(job,creds,stored_path,payload_sha256)
    if options_error:return False,options_error

    # File format validation: non-Bambu printers need .gcode
    if api_type != "bambu" and stored_path.lower().endswith(".3mf"):
        return False, (
            f"{api_type.title()} printers cannot print .3mf files — they need pre-sliced .gcode. "
            "Upload a .gcode file for this model to enable dispatch."
        )

    # Guard 1 — API type compatibility
    printer_name = creds.get("ip", f"printer {printer_id}")
    compat_types = job.get("compatible_api_types") or ""
    if compat_types:
        allowed = [t.strip() for t in compat_types.split(",")]
        if api_type not in allowed:
            return False, (
                f"This file requires a {' or '.join(allowed)} printer, "
                f"but this printer uses {api_type}. "
                "Upload the correct file format for this printer."
            )

    # Guard 2 — Bed size
    file_x = job.get("bed_x_mm")
    file_y = job.get("bed_y_mm")
    printer_x = creds.get("bed_x_mm")
    printer_y = creds.get("bed_y_mm")
    TOLERANCE_MM = 2.0
    if all(v is not None for v in (file_x, file_y, printer_x, printer_y)):
        if file_x > printer_x + TOLERANCE_MM or file_y > printer_y + TOLERANCE_MM:
            return False, (
                f"File was sliced for a {file_x:.0f}x{file_y:.0f}mm bed, "
                f"but this printer has a {printer_x:.0f}x{printer_y:.0f}mm bed. "
                "Upload a re-sliced version."
            )

    # Education jobs fail closed and recompute against live printer/slot facts
    # immediately before any adapter is allowed to touch hardware.
    education_decision = _authorize_education_hardware_action(job, creds)
    if education_decision is not None and not education_decision[0]:
        return education_decision

    if education_decision is not None:
        expected_hash=str(job.get('file_hash') or '').lower()
        if len(expected_hash)!=64 or any(c not in '0123456789abcdef' for c in expected_hash):
            return False,'Education file integrity check failed: stored hash is missing or invalid; no hardware command was sent'
        if not hmac.compare_digest(payload_sha256,expected_hash):
            return False,'Education file integrity check failed: stored contents changed; no hardware command was sent'
    from modules.printers.dispatch_orchestration import dispatch_prepared
    return dispatch_prepared(job,creds,stored_path,remote_filename,payload_sha256,education_decision)


def _run_admitted_dispatch(job,creds,stored_path,remote_filename,print_options,education_provider,education_reservation,*,before_start=None):
    """Existing adapter and post-acceptance recording, after committed admission."""
    job_id=job['id'];printer_id=job['printer_id'];api_type=creds['api_type']
    log.info(
        f"[dispatch] Dispatching job {job_id} ('{job['item_name']}') "
        f"to {api_type} printer {printer_id} @ {creds['ip']}"
    )
    _ws(job_id, "uploading", f"Dispatching '{job['item_name']}'...")

    # Route to the adapter only after the Education reservation is durable.
    uncertain = False
    try:
        if api_type == "bambu":
            success, message = _dispatch_bambu(job_id, stored_path, remote_filename, creds, before_start=before_start, **print_options)
        elif api_type == "moonraker":
            success, message = _dispatch_moonraker(job_id, stored_path, remote_filename, creds)
        else:
            success, message = _dispatch_prusalink(job_id, stored_path, remote_filename, creds)
    except Exception:
        # An unexpected exception may occur after submission. Fail closed rather
        # than removing the only durable fence against a second physical print.
        log.warning("[dispatch] Adapter outcome requires reconciliation for job %s", job_id)
        uncertain = True
        success, message = False, "Printer command outcome is unknown; explicit reconciliation is required before retrying"

    if not success:
        if education_reservation and not uncertain and not _cancel_education_reservation(
            education_provider, education_reservation
        ):
            message += "; Education reservation requires explicit reconciliation"
        _ws(job_id, "failed", message)
        _notify_start_outcome(job, "adapter_unknown" if uncertain else "adapter_rejected")
        return False, message

    if education_reservation:
        confirmation = _confirm_education_dispatch(
            education_provider, education_reservation
        )
        if not confirmation.get("transitioned"):
            message = (
                "Printer accepted the print, but Education authority confirmation failed; "
                "physical action requires reconciliation"
            )
            _ws(job_id, "failed", message)
            _notify_start_outcome(job, "confirmation_failed")
            return False, message
    else:
        # No database lock is held during transport. Revalidate after acceptance
        # so a delayed acknowledgment cannot reopen a completed/cancelled job.
        try:
            from datetime import datetime, timezone
            from core.db import engine
            from core.order_production import prepare_job_write, reconcile_job_write
            from sqlalchemy import text as _text
            reconciliation_message = None
            with engine.begin() as conn:
                parents = prepare_job_write(conn, [job_id])
                from modules.jobs.services import transition_job, Expr
                transitioned = transition_job(conn, job_id, "printing", "dispatch",
                    extra_fields={"actual_start": Expr("COALESCE(actual_start, :now)",
                        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))},
                    where="printer_id = :pid", params={"pid": printer_id}) or None
                reconcile_job_write(conn, parents)
                if transitioned is None:
                    current = conn.execute(_text("SELECT status,printer_id FROM jobs WHERE id=:id"),
                                           {"id":job_id}).first()
                    if current and current[0] == 'completed' and current[1] == printer_id:
                        return True, "Printer accepted the print; newer completed state preserved"
                    message = "Printer accepted the print, but the job changed; physical action requires reconciliation"
                    _ws(job_id, "failed", message)
                    reconciliation_message = message
            if reconciliation_message is not None:
                _notify_start_outcome(job, "confirmation_failed")
                return False, reconciliation_message
        except Exception:
            # Do not expose SQL parameters or credentials in an adapter error.
            log.warning("[dispatch] Accepted print requires database reconciliation for job %s", job_id)
            message = "Printer accepted the print, but recording its state failed; physical action requires reconciliation"
            _ws(job_id, "failed", message)
            _notify_start_outcome(job, "confirmation_failed")
            return False, message

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
