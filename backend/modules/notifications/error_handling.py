"""
Printer error recording and HMS error parsing.

Provides record_error(), clear_error(), parse_hms_errors(), process_hms_errors().
Normalizes error handling across all printer brands.
"""

from core.base import AlertType, AlertSeverity

import json
import logging

# Resolved at call time: never a stale get_db bound at import.
from core.db_live import live_get_db as get_db
from core.db_compat import sql
from modules.notifications.alert_dispatch import dispatch_alert

log = logging.getLogger("printer_events")

from modules.printers.hms_codes import parse_hms_errors
from modules.printers.hms_codes import credential_prefix


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


def process_hms_errors(
    printer_id: int, hms_data: list, observed_filename: str | None = None,
    expected_print_job_id: int | None = None,
    *, tray_evidence_incomplete: bool = False,
):
    """
    Process HMS errors from Bambu printer.
    Creates alerts for new fatal/serious events; never changes job outcomes.
    """
    if not isinstance(hms_data, list):
        return
    try:
        with get_db() as conn:
            row = conn.execute('SELECT api_key,hms_errors FROM printers WHERE id=?', (printer_id,)).fetchone()
        prefix = credential_prefix(row[0]) if row else ''
        previous = json.loads(row[1] or '[]') if row else []
        # Old releases persisted raw packets. Compare identities, not old meanings.
        prior_codes = {e['code'] for e in parse_hms_errors(previous, prefix)} if previous and isinstance(previous[0].get('code'), int) else {e.get('code') for e in previous}
    except Exception:
        log.error('Failed to read HMS state for printer %s; retry required', printer_id)
        return {'retryable': True}
    errors = parse_hms_errors(hms_data, prefix)
    if not errors:
        with get_db() as conn:
            conn.execute('UPDATE printers SET hms_errors=? WHERE id=?', ('[]', printer_id))
            conn.commit()
        return {'errors': []}

    from core.job_timeline import for_printer
    for error in errors:
        code=str(error.get('code', '')).upper()
        try:for_printer(printer_id, 'hms', code=code)
        except ValueError:pass

    # Classify before external printer-error alerts; HMS never ends a job.
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

    # Persist the active set across reconnects/restarts, independent of alert read
    # state and the generic notification policy's six-hour repeat window.
    new_errors = [e for e in errors if e['code'] not in prior_codes]
    for error in new_errors:
        record_error(printer_id=printer_id, error_code=error['code'],
                     error_message=error['message'], source='bambu_hms',
                     severity=error['severity'],
                     create_alert=not education_suppressed and error['severity'] in ('critical', 'error'))
    with get_db() as conn:
        conn.execute('UPDATE printers SET hms_errors=? WHERE id=?', (json.dumps(errors), printer_id))
        conn.commit()
    return {'education_suppressed': education_suppressed, 'errors': errors}
