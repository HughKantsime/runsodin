"""
Printer error recording and HMS error parsing.

Provides record_error(), clear_error(), parse_hms_errors(), process_hms_errors().
Normalizes error handling across all printer brands.
"""

from core.base import AlertType, AlertSeverity

import json
import logging
from typing import Optional
from pathlib import PurePosixPath

from core.order_production import (prepare_job_write, reconcile_job_write, prepare_print_job_write,
                                   ProductionLinkageChanged, retry_production_linkage)
# Resolved at call time: never a stale get_db bound at import.
from core.db_live import live_get_db as get_db
from core.db_compat import sql
from modules.notifications.alert_dispatch import dispatch_alert

log = logging.getLogger("printer_events")

# hms_codes is a pure lookup utility in the printers module — kept as a direct
# import because it has no side effects and no circular dependency risk.
try:
    from modules.printers.hms_codes import lookup_hms_code
except ImportError:
    def lookup_hms_code(code): return f"HMS Error {code}"

# HMS codes that indicate a print-stopping failure. When any of these appear
# while a job is running, the active job is marked as failed even if the
# printer's gcode_state hasn't transitioned to FAILED yet.
# NOTE: Only full-format codes (XXXXXXXX_XXXXXXXX) are listed here because
# parse_hms_errors() produces 17-char codes via f"{attr:08X}_{code:08X}".
# Shorter PRINT_ERROR_CODES (e.g. 0C00_8005) come from a different field.
PRINT_STOPPING_HMS_CODES = {
    '0C000300_00030006',  # Purged filament piled up in waste chute
    '0F010100_00010001',  # Waste chute clogged
    '05010500_00010001',  # AMS filament buffer full / waste chute
    '0C010600_00010001',  # Purge system error / waste chute blocked
    '0C000200_00010001',  # Spaghetti failure detected by AI
    '0C000300_00010002',  # Possible spaghetti failure detected
    '07010200_00010001',  # Spaghetti detection triggered
}


def record_error(
    printer_id: int,
    error_code: str,
    error_message: str,
    source: str = "unknown",  # "bambu_hms", "moonraker", "prusalink", etc.
    severity: str = "warning",  # "info", "warning", "error", "critical"
    create_alert: bool = True,
    history: bool = False,
):
    """
    Record an error from any printer type.
    Normalizes error handling across all brands.
    Optionally creates an alert for users.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()

            # Update printer's last error
            cur.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
                f"""UPDATE printers SET
                    last_error_code = ?,
                    last_error_message = ?,
                    last_error_at = {sql.now()}
                WHERE id = ?""",
                (error_code, error_message, printer_id)
            )

            # Monitors without their own HMS stream (PrusaLink, Klipper) keep
            # error history here; Bambu writes hms_error_history itself.
            if history:
                cur.execute(
                    "INSERT INTO hms_error_history (printer_id, code, message, severity, source) VALUES (?, ?, ?, ?, ?)",
                    (printer_id, error_code, error_message, severity, source),
                )

            # Get printer name for alert
            cur.execute("SELECT name, nickname FROM printers WHERE id = ?", (printer_id,))
            row = cur.fetchone()
            printer_name = row[1] or row[0] if row else f"Printer {printer_id}"

            conn.commit()

        # Create alert if requested (outside DB context)
        if create_alert:
            dispatch_alert(
                alert_type=AlertType.PRINTER_ERROR,
                severity=AlertSeverity.CRITICAL if severity == "error" else AlertSeverity(severity),
                title=f"Error on {printer_name}",
                message=f"[{source}:{error_code}] {error_message}",
                printer_id=printer_id,
                metadata={"source": source, "code": error_code}
            )

        log.warning(f"Printer {printer_id} error [{source}:{error_code}]: {error_message}")

    except Exception as e:
        log.error(f"Failed to record error for printer {printer_id}: {e}")


def clear_error(printer_id: int):
    """Clear the last error after it's resolved."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE printers SET
                    last_error_code = NULL,
                    last_error_message = NULL,
                    last_error_at = NULL
                WHERE id = ?""",
                (printer_id,)
            )
            conn.commit()
    except Exception as e:
        log.error(f"Failed to clear error for printer {printer_id}: {e}")


def parse_hms_errors(hms_data: list) -> list:
    """
    Parse Bambu HMS error array into structured list.
    Returns list of {code, module, severity, message} dicts.
    """
    errors = []

    # HMS severity levels
    SEVERITY_MAP = {
        1: "info",
        2: "warning",
        3: "error",
        4: "critical",
    }

    for item in hms_data or []:
        attr = item.get("attr", 0)
        code = item.get("code", 0)

        # Extract severity from attr (bits 24-27)
        severity_bits = (attr >> 24) & 0xF
        severity = SEVERITY_MAP.get(severity_bits, "warning")

        # Format code as hex string for lookup
        full_code = f"{attr:08X}_{code:08X}"

        errors.append({
            "code": full_code,
            "attr": attr,
            "raw_code": code,
            "severity": severity,
            "message": lookup_hms_code(full_code),
        })

    return errors


def _hms_filename_matches(observed: str | None, stored: str | None) -> bool:
    """Match reported file identity without guessing from display names."""
    from core.interfaces.education_policy import parse_education_token
    if not isinstance(observed, str) or not isinstance(stored, str) or not observed or not stored:
        return False
    observed_token = parse_education_token(observed)
    stored_token = parse_education_token(stored)
    if observed_token is not None or stored_token is not None:
        return observed_token is not None and observed_token == stored_token
    return PurePosixPath(observed.replace('\\', '/')).name == PurePosixPath(stored.replace('\\', '/')).name


@retry_production_linkage
def _fail_active_job_for_hms(printer_id: int, hms_code: str, hms_message: str, observed_filename: str | None = None, expected_print_job_id: int | None = None, *, tray_evidence_incomplete: bool = False):
    """Mark the active print job as failed due to a print-stopping HMS error.

    Checks for an active print_jobs record on this printer. If found, marks it
    failed, updates the linked jobs record, creates a print archive, and
    dispatches a failure alert via Path B (webhooks/push/email).
    """
    committed = False
    try:
        with get_db() as conn:
            cur = conn.cursor()

            statement = "SELECT id,scheduled_job_id,job_name,started_at,filename FROM print_jobs WHERE printer_id=? AND status='running' ORDER BY started_at DESC,id DESC LIMIT 1"
            parameters = (printer_id,)
            if expected_print_job_id is not None:
                statement = "SELECT id,scheduled_job_id,job_name,started_at,filename FROM print_jobs WHERE printer_id=? AND id=? AND status='running'"
                parameters = (printer_id, expected_print_job_id)
            row = cur.execute(statement, parameters).fetchone()
            if not row:
                return {"terminal_outcome": "irrelevant"}
            print_job_id, scheduled_job_id, job_name, started_at, filename = row

            from core.registry import registry
            from core.interfaces.education_policy import is_education_reserved_filename, parse_education_token
            policy = registry.get_provider("EducationPolicyProvider")
            reserved = is_education_reserved_filename(observed_filename) or is_education_reserved_filename(filename)
            if not policy and reserved:
                return {"terminal_outcome": "retryable"}
            classification = policy.classify_monitor_observation(
                conn, printer_id=printer_id, print_job_id=print_job_id,
                observed_filename=observed_filename,
            ) if policy else {}
            if classification.get("education_owned") or classification.get("education_reserved") or reserved:
                if not policy or parse_education_token(observed_filename) is None:
                    return {"terminal_outcome": "irrelevant"}
                # A linked print alone does not prove that this packet belongs to it.
                # Resolve the incoming token independently through canonical claims.
                authority = policy.classify_monitor_observation(
                    conn, printer_id=printer_id, observed_filename=observed_filename,
                )
                if not authority.get("authorized") or not classification.get("education_owned") or any(
                    authority.get(key) != classification.get(key)
                    for key in ("submission_id", "job_id", "lifecycle_revision")
                ):
                    return {"terminal_outcome": "irrelevant"}
                education_result = policy.terminal_monitor_observation(
                    conn, print_job_id=print_job_id, printer_id=printer_id,
                    terminal_status="failed", error_code=hms_code, observed_filename=observed_filename,
                )
                # Require saved failure, not merely successful classification.
                saved = cur.execute("SELECT status FROM print_jobs WHERE id=?", (print_job_id,)).fetchone()
                if not saved or saved[0] != 'failed':
                    return {"terminal_outcome": "retryable"}
                from core.job_terminal_observations import terminal_event
                timeline_item = terminal_event(conn, print_job_id) if (education_result.get('physical_ended') or education_result.get('transitioned')) else None
                if tray_evidence_incomplete:
                    from core.interfaces.attempt_accounting import attempt_accounting
                    attempt_accounting().mark_attempt_tray_evidence_incomplete(conn, print_job_id, printer_id)
                conn.commit()
                from core.job_timeline import enqueue
                if timeline_item is not None:enqueue(timeline_item)
                committed = True
                return {**education_result, "terminal_outcome": "committed"}

            # Production parents precede the print row. Education policy must
            # never run while this generic child-row fence is held.
            scheduled_job_id, production_orders = prepare_print_job_write(conn, print_job_id)
            row = cur.execute(statement, parameters).fetchone()
            if not row or row[0] != print_job_id:
                return {"terminal_outcome": "irrelevant"}
            print_job_id, locked_job_id, job_name, started_at, filename = row
            if locked_job_id != scheduled_job_id:
                raise ProductionLinkageChanged()
            if policy:
                locked_classification = policy.classify_monitor_observation(
                    conn, printer_id=printer_id, print_job_id=print_job_id,
                    observed_filename=observed_filename,
                )
                if locked_classification.get("education_owned") or locked_classification.get("education_reserved"):
                    conn.rollback()
                    raise ProductionLinkageChanged()
            if is_education_reserved_filename(filename):
                conn.rollback()
                raise ProductionLinkageChanged()
            if not _hms_filename_matches(observed_filename, filename):
                return {"terminal_outcome": "irrelevant"}

            if tray_evidence_incomplete:
                from core.interfaces.attempt_accounting import attempt_accounting
                attempt_accounting().mark_attempt_tray_evidence_incomplete(conn, print_job_id, printer_id)

            # Mark print_jobs as failed (WHERE status='running' guards against races)
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — params bound via ?, f-string interpolates only sql.* dialect helpers or allowlisted symbols
            cur.execute(f"""
                UPDATE print_jobs
                SET status = 'failed', ended_at = {sql.now()}, error_code = ?
                WHERE id = ? AND status = 'running'
            """, (hms_code, print_job_id))

            if cur.rowcount == 0:
                return {"terminal_outcome": "retryable"}  # Reconcile uncertain winner

            # Mark linked scheduled job as failed
            if scheduled_job_id:
                from modules.jobs.services import transition_job, Expr
                transition_job(cur, scheduled_job_id, 'failed', 'monitor',
                               extra_fields={'actual_end': Expr(sql.now())})

            from core.job_terminal_observations import claim_terminal, terminal_event
            claim_terminal(conn, print_job_id)
            timeline_item=terminal_event(conn, print_job_id)
            reconcile_job_write(conn, production_orders)
            conn.commit()
            from core.job_timeline import enqueue
            if timeline_item is not None:enqueue(timeline_item)
            committed = True

        log.warning(f"Printer {printer_id}: HMS {hms_code} failed active job "
                    f"{print_job_id} ({job_name})")

        # Archive the failed print
        try:
            from modules.archives.archive import create_print_archive
            create_print_archive(print_job_id, printer_id, success=False)
        except Exception as e:
            log.error(f"Failed to create print archive for job {print_job_id}: {e}")

        # Deliver the terminal claim once through the durable outbox.
        from core.job_terminal_observations import dispatch_terminal_alert
        dispatch_terminal_alert(print_job_id, dispatch_alert,
            alert_type=AlertType.PRINT_FAILED,
            severity=AlertSeverity.CRITICAL,
            title=f"Print Failed (HMS): {job_name or 'Unknown'}",
            message=f"Active job failed due to HMS error: {hms_message}",
            printer_id=printer_id,
            job_id=scheduled_job_id,
            metadata={"hms_code": hms_code, "reason": "hms_error"}
        )

        return {"terminal_outcome": "committed"}

    except ProductionLinkageChanged:
        raise
    except Exception:
        log.error("Failed to check/fail active job for HMS on printer %s; reconciliation required", printer_id)

        return {"terminal_outcome": "committed" if committed else "retryable"}


def process_hms_errors(
    printer_id: int, hms_data: list, observed_filename: str | None = None,
    expected_print_job_id: int | None = None,
    *, tray_evidence_incomplete: bool = False,
):
    """
    Process HMS errors from Bambu printer.
    Creates alerts for new errors. Fails active jobs for print-stopping codes.
    """
    errors = parse_hms_errors(hms_data)

    if not errors:
        # No errors - clear any existing
        clear_error(printer_id)
        return

    # Store JSON in hms_errors column
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE printers SET hms_errors = ? WHERE id = ?",
                (json.dumps(errors), printer_id)
            )
            conn.commit()
    except Exception as e:
        log.error(f"Failed to store HMS errors for printer {printer_id}: {e}")

    from core.job_timeline import for_printer
    for error in errors:
        code=str(error.get('code', '')).upper()
        try:for_printer(printer_id, 'hms', code=code)
        except ValueError:pass

    # Classify before either the outer printer-error alert or print-failure path.
    # Local sanitized telemetry/history remains allowed for Education.
    from core.interfaces.education_policy import is_education_reserved_filename

    education_suppressed = is_education_reserved_filename(observed_filename)
    try:
        from core.registry import registry
        policy = registry.get_provider("EducationPolicyProvider")
        with get_db() as conn:
            if expected_print_job_id is None:
                row = conn.execute(
                    "SELECT id,filename,job_name FROM print_jobs "
                    "WHERE printer_id=? AND status='running' ORDER BY started_at DESC,id DESC LIMIT 1",
                    (printer_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id,filename,job_name FROM print_jobs WHERE printer_id=? AND id=? AND status='running'",
                    (printer_id, expected_print_job_id),
                ).fetchone()
                if row is None:
                    return {"education_suppressed": education_suppressed, "errors": errors,
                            "terminal_outcome": "irrelevant"}
            education_suppressed = education_suppressed or is_education_reserved_filename(row[1] if row else None)
            if not policy and education_suppressed:
                raise RuntimeError("Reserved observation requires Education policy")
            if policy:
                classification = policy.classify_monitor_observation(
                    conn, printer_id=printer_id, print_job_id=row[0] if row else None,
                    observed_filename=observed_filename or (row[1] if row else None),
                )
                if not isinstance(classification, dict) or not any(key in classification for key in ("education_owned", "education_reserved")):
                    raise RuntimeError("Missing classification outcome")
                if any(not isinstance(classification.get(key, False), bool) for key in ("education_owned", "education_reserved")):
                    raise RuntimeError("Invalid classification outcome")
                education_suppressed = education_suppressed or classification.get("education_owned", False) or classification.get("education_reserved", False)
    except Exception:
        log.error("Failed to classify HMS observation for printer %s; retry required", printer_id)
        # No external alert or terminal operation has started. Retrying this
        # unresolved classification cannot repeat partially delivered effects.
        return {"education_suppressed": True, "errors": errors,
                "classification_state": "unresolved", "retryable": True}

    # Create an external alert only for generic observations.
    worst = max(errors, key=lambda e: {"info": 0, "warning": 1, "error": 2, "critical": 3}.get(e["severity"], 0))
    record_error(
        printer_id=printer_id,
        error_code=worst["code"],
        error_message=worst["message"],
        source="bambu_hms",
        severity=worst["severity"],
        create_alert=not education_suppressed
    )

    # Check if any HMS code should fail the active print job
    for err in errors:
        if err["code"] in PRINT_STOPPING_HMS_CODES:
            outcome = _fail_active_job_for_hms(printer_id, err["code"], err["message"], observed_filename=observed_filename, expected_print_job_id=expected_print_job_id, tray_evidence_incomplete=tray_evidence_incomplete)
            if not isinstance(outcome, dict) or outcome.get("terminal_outcome") == "retryable":
                return {"education_suppressed": education_suppressed, "errors": errors,
                        "classification_state": "unresolved", "retryable": True}
            break  # Only fail once per HMS batch

    return {
        "education_suppressed": education_suppressed,
        "errors": errors,
    }
