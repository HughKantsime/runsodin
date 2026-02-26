"""
Printer error recording and HMS error parsing.

Provides record_error(), clear_error(), parse_hms_errors(), process_hms_errors().
Normalizes error handling across all printer brands.
"""

import json
import logging
from typing import Optional

from core.db_utils import get_db
from modules.notifications.alert_dispatch import dispatch_alert

log = logging.getLogger("printer_events")

# hms_codes is a pure lookup utility in the printers module — kept as a direct
# import because it has no side effects and no circular dependency risk.
try:
    from modules.printers.hms_codes import lookup_hms_code
except ImportError:
    def lookup_hms_code(code): return f"HMS Error {code}"


def record_error(
    printer_id: int,
    error_code: str,
    error_message: str,
    source: str = "unknown",  # "bambu_hms", "moonraker", "prusalink", etc.
    severity: str = "warning",  # "info", "warning", "error", "critical"
    create_alert: bool = True,
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
            cur.execute(
                """UPDATE printers SET
                    last_error_code = ?,
                    last_error_message = ?,
                    last_error_at = datetime('now')
                WHERE id = ?""",
                (error_code, error_message, printer_id)
            )

            # Get printer name for alert
            cur.execute("SELECT name, nickname FROM printers WHERE id = ?", (printer_id,))
            row = cur.fetchone()
            printer_name = row[1] or row[0] if row else f"Printer {printer_id}"

            conn.commit()

        # Create alert if requested (outside DB context)
        if create_alert:
            dispatch_alert(
                alert_type="printer_error",
                severity=severity,
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


def process_hms_errors(printer_id: int, hms_data: list):
    """
    Process HMS errors from Bambu printer.
    Creates alerts for new errors.
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

    # Create alert for most severe error
    worst = max(errors, key=lambda e: {"info": 0, "warning": 1, "error": 2, "critical": 3}.get(e["severity"], 0))
    record_error(
        printer_id=printer_id,
        error_code=worst["code"],
        error_message=worst["message"],
        source="bambu_hms",
        severity=worst["severity"],
        create_alert=True
    )
