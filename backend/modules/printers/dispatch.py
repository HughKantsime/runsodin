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
import hmac
import json
import logging
import time

log = logging.getLogger("odin.dispatch")


class DispatchOutcomeUncertain(RuntimeError):
    """A print-capable operation was entered without reliable confirmation."""

def _submit_print_command(operation, *args, **kwargs):
    """Do not turn a lost acknowledgment into permission to send again."""
    from modules.printers.maintenance_gate import check_command
    check_command()
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
        from modules.printers.dispatch_background import progress
        phases = {'uploading': 'uploading', 'starting': 'starting'}
        if status in phases:progress(phases[status])
    except Exception:
        log.debug('Dispatch progress update failed')
    try:
        if status in ('uploading','starting','dispatched','failed'):
            from core.job_timeline import enqueue, event
            enqueue(event(job_id, 'dispatch_'+status))
    except Exception:
        log.debug('Timeline enqueue failed')
    try:
        _ws_push("job_dispatch_event", {"job_id": job_id, "status": status, "message": message})
    except Exception as e:
        log.debug(f"WebSocket push failed: {e}")



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
    except Exception as exc:
        from modules.printers.dispatch_ams_gate import record_refusal
        return record_refusal(before_start, "exception:" + type(exc).__name__, "Post-upload gate raised an exception")


def _prepare_post_upload_gate(before_start):
    if before_start is None:
        return True
    prepare = getattr(before_start, 'prepare', None)
    if prepare is None:
        return True
    try:
        return callable(prepare) and prepare() is True
    except Exception as exc:
        from modules.printers.dispatch_ams_gate import record_refusal
        return record_refusal(before_start, "exception:" + type(exc).__name__, "Post-upload preparation raised an exception")


def _gate_refusal_message(gate):
    from modules.printers.dispatch_ams_gate import GateRefusal, record_refusal
    record_refusal(gate, "gate_rejected", "Post-upload callback refused print start")
    try:
        refusal = getattr(gate, "refusal", None)
    except Exception:
        refusal = None
    if refusal is None:
        refusal = GateRefusal("gate_rejected", "Post-upload callback refused print start")
    detail = refusal.detail.removeprefix(refusal.code + ": ")
    return ("Post-upload printer and AMS checks refused print start "
            f"({refusal.code}: {detail}); no print command was sent")


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
    if not _prepare_post_upload_gate(before_start):
        return False, _gate_refusal_message(before_start)
    log.info(f"[dispatch] Sending Bambu V2 start_print for '{remote_filename}'")
    final_check = {"before_command": before_start} if before_start is not None else {}
    try:
        ok = _submit_print_command(run_command, config, "start_print", remote_filename, **final_check, **print_options)
    except DispatchOutcomeUncertain:
        if getattr(before_start, "refusal", None) is not None:
            raise DispatchOutcomeUncertain(_gate_refusal_message(before_start)) from None
        raise
    if not ok:
        if getattr(before_start, "refusal", None) is not None:
            return False, _gate_refusal_message(before_start)
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
        if not _prepare_post_upload_gate(before_start):
            return False, _gate_refusal_message(before_start)
        if not printer.connect():
            return False, "MQTT connection failed after successful file upload"

        time.sleep(1)
        if not _post_upload_gate(before_start):
            return False, _gate_refusal_message(before_start)
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


def _notify_start_outcome(job: dict, reason: str, *, gate_refusal=None, message=None):
    """Record a safe outcome without changing or repeating physical dispatch."""
    if gate_refusal is not None:
        from modules.printers.maintenance_gate import record_code
        record_code(gate_refusal)
        from core.job_timeline import enqueue, event
        code = str(gate_refusal.code).upper()
        try:enqueue(event(job['id'], 'gate_refused', printer_id=job.get('printer_id'), code=code, detail={'gate': code[:64]}))
        except Exception:pass
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
                "Print start needs attention", message if gate_refusal else messages[reason], job_id=job["id"],
                printer_id=job["printer_id"], metadata={"reason": reason, **({"gate_refusal_code": gate_refusal.code} if gate_refusal else {})}, target_user_ids=[creator])
            db.commit()
            deliver_staged_alert(db, plan)
    except Exception:
        log.warning("Dispatch outcome notification failed; physical result preserved")

from modules.printers.dispatch_options import _validated_print_options


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
    # Bambu checks selected-plate geometry at its post-upload capability gate;
    # a slicer's entire printable-area profile is not the object's extent.
    if api_type != 'bambu' and all(v is not None for v in (file_x, file_y, printer_x, printer_y)):
        if file_x > printer_x + TOLERANCE_MM or file_y > printer_y + TOLERANCE_MM:
            return False, (
                f"bed_geometry_mismatch: File was sliced for a {file_x:.0f}x{file_y:.0f}mm bed, "
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
    from modules.printers.maintenance_gate import MaintenanceHold
    try:
        if api_type == "bambu":
            success, message = _dispatch_bambu(job_id, stored_path, remote_filename, creds, before_start=before_start, **print_options)
        elif api_type == "moonraker":
            success, message = _dispatch_moonraker(job_id, stored_path, remote_filename, creds)
        else:
            success, message = _dispatch_prusalink(job_id, stored_path, remote_filename, creds)
    except MaintenanceHold as exc:
        success, message = False, str(exc)
    except Exception:
        # An unexpected exception may occur after submission. Fail closed rather
        # than removing the only durable fence against a second physical print.
        log.warning("[dispatch] Adapter outcome requires reconciliation for job %s", job_id)
        uncertain = True
        success, message = False, "Printer command outcome is unknown; explicit reconciliation is required before retrying"
        if getattr(before_start, "refusal", None) is not None:
            message = _gate_refusal_message(before_start) + "; explicit reconciliation is required before retrying"

    if not success:
        if before_start is not None:
            try:
                before_start.clean_no_start = not uncertain and getattr(before_start, 'refusal', None) is not None
            except (AttributeError, TypeError):
                pass
        if education_reservation and not uncertain and not _cancel_education_reservation(
            education_provider, education_reservation
        ):
            try:
                before_start.clean_no_start = False
            except (AttributeError, TypeError):
                pass
            message += "; Education reservation requires explicit reconciliation"
        _ws(job_id, "failed", message)
        refusal = getattr(before_start, "refusal", None)
        if refusal is not None:
            log.warning("Post-upload gate refused job_id=%s printer_id=%s code=%s detail=%s",
                        job_id, printer_id, refusal.code, refusal.detail)
            _notify_start_outcome(job, "adapter_unknown" if uncertain else "adapter_rejected", gate_refusal=refusal, message=message)
        else:
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
        if job.get('education_submission_id') is not None:
            # Refusal of an Education head cannot starve generic work. Every
            # fallback still enters the complete dispatch gates and target lock;
            # an uncertain send/claim therefore continues to block all sends.
            fallback = _get_next_scheduled_job(printer_id, generic_only=True)
            if fallback:
                dispatch_job(printer_id, fallback['id'])

from modules.printers.dispatch_loading import (_get_printer_info, _get_next_scheduled_job, _load_job)

from modules.printers.dispatch_education import (_reconcile_education_denial, _authorize_education_hardware_action, _reserve_education_hardware_action, _cancel_education_reservation, _confirm_education_dispatch)


def stage_dispatch(printer_id, job_id):
    """Sanctioned route entry; same dispatch authority, acknowledged at durable claim."""
    from modules.printers.dispatch_background import stage
    return stage(printer_id, job_id)


def read_dispatch_progress(db, token, job_id):
    """Read-only attempt details; caller must enforce job and printer visibility."""
    from modules.printers.dispatch_background import progress_key, result_key, active
    from modules.printers.start_next import load
    record = load(db, progress_key(token)) or load(db, result_key(job_id))
    if not record or record['attempt_id'] != token:
        return None
    from sqlalchemy import text
    claim_state = db.execute(text('SELECT state FROM printer_dispatch_claims WHERE token=:token AND job_id=:job'),
                             {'token': token, 'job': job_id}).scalar_one_or_none()
    running = active(token)
    if claim_state and (claim_state != 'adapter_entered' or not running):
        record['phase'] = {'preparing': 'preparing' if running else 'needs_reconciliation',
            'adapter_entered': 'needs_reconciliation', 'accepted': 'start_accepted',
            'uncertain': 'needs_reconciliation'}[claim_state]
    elif not claim_state and not running and record['phase'] not in ('start_accepted', 'refused', 'needs_reconciliation'):
        record['phase'] = 'unconfirmed'
    return record
