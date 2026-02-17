"""
Universal Printer Event Handler

Shared module for all printer monitors (Bambu MQTT, Moonraker, PrusaLink, etc.)
Provides consistent event handling, care counter updates, and alert dispatch.

All monitors import this and call these functions instead of writing
their own DB logic. This ensures consistent behavior across printer brands.
"""

import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from db_utils import get_db

log = logging.getLogger("printer_events")

try:
    import mqtt_republish
except ImportError:
    mqtt_republish = None

try:
    from hms_codes import lookup_hms_code
except ImportError:
    def lookup_hms_code(code): return f"HMS Error {code}"

try:
    import smart_plug
except ImportError:
    smart_plug = None

try:
    from ws_hub import push_event as ws_push
except ImportError:
    def ws_push(*a, **kw): pass


# =============================================================================
# TELEMETRY EVENTS
# =============================================================================

def send_push_notification(user_id: int, alert_type: str, title: str, message: str,
                           alert_id: int = None, printer_id: int = None, job_id: int = None):
    """Send push notification to user's subscribed devices."""
    import json

    with get_db() as conn:
        cur = conn.cursor()

        try:
            # Get user's push subscriptions
            cur.execute("SELECT endpoint, p256dh_key, auth_key FROM push_subscriptions WHERE user_id = ?", (user_id,))
            subscriptions = cur.fetchall()

            if not subscriptions:
                return

            # Get VAPID keys
            cur.execute("SELECT value FROM system_config WHERE key = 'vapid_keys'")
            vapid_row = cur.fetchone()
            if not vapid_row:
                log.warning("VAPID keys not configured, skipping push")
                return

            vapid_keys = json.loads(vapid_row[0])

            # Build notification payload
            payload = json.dumps({
                "title": title,
                "body": message,
                "alert_type": alert_type,
                "alert_id": alert_id,
                "printer_id": printer_id,
                "job_id": job_id,
                "url": "/alerts"
            })

            # Send to each subscription
            try:
                from pywebpush import webpush, WebPushException

                for endpoint, p256dh, auth in subscriptions:
                    try:
                        webpush(
                            subscription_info={
                                "endpoint": endpoint,
                                "keys": {"p256dh": p256dh, "auth": auth}
                            },
                            data=payload,
                            vapid_private_key=vapid_keys["private_key"],
                            vapid_claims={"sub": "mailto:admin@runsodin.com"}
                        )
                        log.info(f"Push sent to user {user_id}")
                    except WebPushException as e:
                        if e.response and e.response.status_code in (404, 410):
                            # Subscription expired, remove it
                            cur.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
                            conn.commit()
                            log.info(f"Removed expired push subscription")
                        else:
                            log.error(f"Push failed: {e}")
                    except Exception as e:
                        log.error(f"Push error: {e}")
            except ImportError:
                log.debug("pywebpush not installed, skipping push notifications")

        except Exception as e:
            log.error(f"Failed to send push notification: {e}")


def send_webhook(alert_type: str, title: str, message: str, severity: str = "info",
                 printer_id: int = None, job_id: int = None):
    """Send alert to configured webhooks."""
    import json

    with get_db() as conn:
        cur = conn.cursor()

        # Get enabled webhooks that match this alert type
        cur.execute("SELECT id, url, webhook_type, alert_types FROM webhooks WHERE is_enabled = 1")
        webhooks = cur.fetchall()

        for webhook_id, url, webhook_type, alert_types_json in webhooks:
            # Check if this webhook wants this alert type
            if alert_types_json:
                try:
                    allowed_types = json.loads(alert_types_json)
                    if alert_type not in allowed_types:
                        continue
                except Exception:
                    pass

            # Build payload based on webhook type
            try:
                import httpx

                # Color based on severity
                colors = {"info": 0x3498db, "warning": 0xf39c12, "error": 0xe74c3c, "critical": 0x9b59b6}
                color = colors.get(severity, 0x3498db)

                if webhook_type == "discord":
                    payload = {
                        "embeds": [{
                            "title": f"🖨️ {title}",
                            "description": message,
                            "color": color,
                            "footer": {"text": "O.D.I.N."},
                            "timestamp": datetime.now(timezone.utc).isoformat() + "Z"
                        }]
                    }
                else:  # slack
                    emoji = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}.get(severity, "📢")
                    payload = {
                        "blocks": [
                            {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {title}"}},
                            {"type": "section", "text": {"type": "mrkdwn", "text": message}}
                        ]
                    }

                httpx.post(url, json=payload, timeout=5)
                log.debug(f"Webhook sent to {webhook_type}")

            except Exception as e:
                log.error(f"Webhook failed: {e}")


def send_email(user_id: int, alert_type: str, title: str, message: str,
               printer_id: int = None, job_id: int = None):
    """Send email notification to user."""
    import json
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    try:
        with get_db() as conn:
            cur = conn.cursor()

            # Get SMTP config
            cur.execute("SELECT value FROM system_config WHERE key = 'smtp_config'")
            smtp_row = cur.fetchone()
            if not smtp_row:
                log.debug("SMTP not configured, skipping email")
                return

            smtp_config = json.loads(smtp_row[0])
            if not smtp_config.get("enabled"):
                return

            # Get user email
            cur.execute("SELECT email FROM users WHERE id = ?", (user_id,))
            user_row = cur.fetchone()
            if not user_row or not user_row[0]:
                log.debug(f"No email for user {user_id}")
                return

            user_email = user_row[0]

        # Build email (outside DB context — no longer need conn)
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"🖨️ O.D.I.N.: {title}"
        msg['From'] = smtp_config.get("from_address", smtp_config.get("username"))
        msg['To'] = user_email

        # Plain text version
        text_body = f"{title}\n\n{message}\n\n--\nO.D.I.N."

        # HTML version
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px; background: #1a1a1a; color: #e0e0e0;">
            <div style="max-width: 500px; margin: 0 auto; background: #2a2a2a; padding: 20px; border-radius: 8px;">
                <h2 style="color: #3b82f6; margin-top: 0;">🖨️ {title}</h2>
                <p style="color: #d0d0d0;">{message}</p>
                <hr style="border: none; border-top: 1px solid #444; margin: 20px 0;">
                <p style="color: #888; font-size: 12px;">O.D.I.N.</p>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(text_body, 'plain'))
        msg.attach(MIMEText(html_body, 'html'))

        # Send email
        host = smtp_config.get("host", "localhost")
        port = int(smtp_config.get("port", 587))
        username = smtp_config.get("username", "")
        password = smtp_config.get("password", "")

        with smtplib.SMTP(host, port, timeout=10) as server:
            server.starttls()
            if username and password:
                server.login(username, password)
            server.send_message(msg)

        log.info(f"Email sent to {user_email} for alert {alert_type}")

    except Exception as e:
        log.error(f"Failed to send email: {e}")


def update_telemetry(
    printer_id: int,
    bed_temp: float = None,
    bed_target: float = None,
    nozzle_temp: float = None,
    nozzle_target: float = None,
    state: str = None,
    stage: str = None,
    progress_percent: int = None,
    remaining_minutes: int = None,
    current_layer: int = None,
    total_layers: int = None,
    # Brand-specific (optional)
    lights_on: bool = None,
    nozzle_type: str = None,
    nozzle_diameter: float = None,
    hms_errors: str = None,
):
    """
    Update printer telemetry in database.
    Called by all monitors on each status update.
    Only updates fields that are provided (not None).
    """
    updates = ["last_seen = datetime('now')"]
    params = []

    field_map = {
        "bed_temp": bed_temp,
        "bed_target_temp": bed_target,
        "nozzle_temp": nozzle_temp,
        "nozzle_target_temp": nozzle_target,
        "gcode_state": state,
        "print_stage": stage,
        "lights_on": lights_on,
        "nozzle_type": nozzle_type,
        "nozzle_diameter": nozzle_diameter,
        "hms_errors": hms_errors,
    }

    for col, val in field_map.items():
        if val is not None:
            updates.append(f"{col} = ?")
            params.append(val)

    params.append(printer_id)

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE printers SET {', '.join(updates)} WHERE id = ?",
                params
            )
            conn.commit()
    except Exception as e:
        log.error(f"Failed to update telemetry for printer {printer_id}: {e}")


def mark_online(printer_id: int):
    """Mark printer as online (update last_seen)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE printers SET last_seen = datetime('now') WHERE id = ?",
                (printer_id,)
            )
            conn.commit()
    except Exception as e:
        log.error(f"Failed to mark printer {printer_id} online: {e}")


def mark_offline(printer_id: int):
    """Mark printer as offline (clear state)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE printers SET
                    gcode_state = 'offline',
                    print_stage = NULL
                WHERE id = ?""",
                (printer_id,)
            )
            conn.commit()
    except Exception as e:
        log.error(f"Failed to mark printer {printer_id} offline: {e}")


# =============================================================================
# CAMERA DISCOVERY
# =============================================================================

def discover_camera(printer_id: int, rtsp_url: str):
    """
    Auto-populate camera URL if not already set.
    Called when Bambu X1C broadcasts ipcam.rtsp_url in MQTT.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()

            # Check if camera_url is already set
            cur.execute("SELECT camera_url, camera_discovered FROM printers WHERE id = ?", (printer_id,))
            row = cur.fetchone()

            if row and not row[0]:  # camera_url is empty
                cur.execute(
                    """UPDATE printers SET
                        camera_url = ?,
                        camera_discovered = 1
                    WHERE id = ?""",
                    (rtsp_url, printer_id)
                )
                conn.commit()
                log.info(f"Auto-discovered camera for printer {printer_id}: {rtsp_url}")

                # Regenerate go2rtc config so the new camera stream is available
                try:
                    from main import sync_go2rtc_config_standalone
                    sync_go2rtc_config_standalone()
                    log.info(f"go2rtc config synced after camera discovery for printer {printer_id}")
                except Exception as e2:
                    log.warning(f"Could not sync go2rtc config: {e2}")
    except Exception as e:
        log.error(f"Failed to discover camera for printer {printer_id}: {e}")


# =============================================================================
# CARE COUNTERS
# =============================================================================

def increment_care_counters(printer_id: int, print_hours: float, print_count: int = 1):
    """
    Increment care counters after job completion.
    Called by all monitors when a print finishes successfully.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE printers SET
                    total_print_hours = COALESCE(total_print_hours, 0) + ?,
                    total_print_count = COALESCE(total_print_count, 0) + ?,
                    hours_since_maintenance = COALESCE(hours_since_maintenance, 0) + ?,
                    prints_since_maintenance = COALESCE(prints_since_maintenance, 0) + ?
                WHERE id = ?""",
                (print_hours, print_count, print_hours, print_count, printer_id)
            )
            conn.commit()
        log.debug(f"Incremented care counters for printer {printer_id}: +{print_hours:.2f}h, +{print_count} prints")
        # Also increment nozzle lifecycle counters
        increment_nozzle_lifecycle(printer_id, print_hours, print_count)
    except Exception as e:
        log.error(f"Failed to increment care counters for printer {printer_id}: {e}")


def increment_nozzle_lifecycle(printer_id: int, print_hours: float, print_count: int = 1):
    """Increment the current (active) nozzle's usage counters after job completion."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE nozzle_lifecycle SET
                    print_hours_accumulated = print_hours_accumulated + ?,
                    print_count = print_count + ?
                WHERE printer_id = ? AND removed_at IS NULL""",
                (print_hours, print_count, printer_id)
            )
            conn.commit()
    except Exception as e:
        log.debug(f"Nozzle lifecycle update for printer {printer_id}: {e}")


def reset_maintenance_counters(printer_id: int):
    """
    Reset maintenance counters after maintenance is performed.
    Called from maintenance API endpoint.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE printers SET
                    hours_since_maintenance = 0,
                    prints_since_maintenance = 0
                WHERE id = ?""",
                (printer_id,)
            )
            conn.commit()
        log.info(f"Reset maintenance counters for printer {printer_id}")
    except Exception as e:
        log.error(f"Failed to reset maintenance counters for printer {printer_id}: {e}")


# =============================================================================
# ERROR HANDLING
# =============================================================================

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


# =============================================================================
# HMS ERROR PARSING (Bambu-specific, but called through universal interface)
# =============================================================================

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


# =============================================================================
# JOB EVENTS
# =============================================================================

def job_started(
    printer_id: int,
    job_name: str,
    total_layers: int = None,
    scheduled_job_id: int = None,
):
    """
    Called when a print job starts on any printer.
    Returns the print_jobs.id for tracking.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO print_jobs
                    (printer_id, job_name, started_at, status, total_layers, scheduled_job_id)
                VALUES (?, ?, datetime('now'), 'running', ?, ?)""",
                (printer_id, job_name, total_layers, scheduled_job_id)
            )
            job_id = cur.lastrowid
            conn.commit()

        log.info(f"Job started on printer {printer_id}: {job_name} (print_jobs.id={job_id})")

        # Smart plug: auto power-on
        if smart_plug:
            try:
                smart_plug.on_print_start(printer_id)
            except Exception as e:
                log.warning(f"Smart plug on_print_start failed: {e}")
        ws_push("job_started", {
            "printer_id": printer_id,
            "job_name": job_name,
            "print_job_id": job_id,
        })
        return job_id

    except Exception as e:
        log.error(f"Failed to record job start for printer {printer_id}: {e}")
        return None


def job_completed(
    printer_id: int,
    print_job_id: int,
    success: bool = True,
    duration_seconds: float = None,
    fail_reason: str = None,
):
    """
    Called when a print job finishes on any printer.
    Updates care counters if successful.
    Creates alerts for completion/failure.
    """
    status = "completed" if success else "failed"

    try:
        with get_db() as conn:
            cur = conn.cursor()

            # Update print_jobs record
            cur.execute(
                """UPDATE print_jobs SET
                    status = ?,
                    ended_at = datetime('now')
                WHERE id = ?""",
                (status, print_job_id)
            )

            # Get job details for alert
            cur.execute(
                "SELECT job_name, scheduled_job_id FROM print_jobs WHERE id = ?",
                (print_job_id,)
            )
            row = cur.fetchone()
            job_name = row[0] if row else "Unknown"
            scheduled_job_id = row[1] if row else None

            # Get printer name
            cur.execute("SELECT name, nickname FROM printers WHERE id = ?", (printer_id,))
            prow = cur.fetchone()
            printer_name = prow[1] or prow[0] if prow else f"Printer {printer_id}"

            # Update scheduled job status if linked
            if scheduled_job_id:
                cur.execute(
                    "UPDATE jobs SET status = ? WHERE id = ?",
                    (status, scheduled_job_id)
                )

            conn.commit()

        # Increment care counters if successful
        if success and duration_seconds:
            print_hours = duration_seconds / 3600.0
            increment_care_counters(printer_id, print_hours, 1)

        # Create alert
        if success:
            dispatch_alert(
                alert_type="print_complete",
                severity="success",
                title=f"Print Complete: {job_name}",
                message=f"Finished on {printer_name}",
                printer_id=printer_id,
                job_id=scheduled_job_id,
            )
        else:
            dispatch_alert(
                alert_type="print_failed",
                severity="error",
                title=f"Print Failed: {job_name}",
                message=f"Failed on {printer_name}" + (f": {fail_reason}" if fail_reason else ""),
                printer_id=printer_id,
                job_id=scheduled_job_id,
            )

            # Also record as error
            record_error(
                printer_id=printer_id,
                error_code="PRINT_FAILED",
                error_message=fail_reason or "Print failed",
                source="job",
                severity="error",
                create_alert=False,  # Already created above
            )

        log.info(f"Job {status} on printer {printer_id}: {job_name}")

        # Smart plug: auto power-off (with cooldown)
        if smart_plug and success:
            try:
                smart_plug.on_print_complete(printer_id)
            except Exception as e:
                log.warning(f"Smart plug on_print_complete failed: {e}")
        ws_push("job_completed", {
            "printer_id": printer_id,
            "job_name": job_name,
            "status": status,
            "print_job_id": print_job_id,
            "scheduled_job_id": scheduled_job_id,
        })

    except Exception as e:
        log.error(f"Failed to record job completion for printer {printer_id}: {e}")


def update_job_progress(
    print_job_id: int,
    progress_percent: int = None,
    remaining_minutes: int = None,
    current_layer: int = None,
):
    """Update progress for an active print job."""
    updates = []
    params = []

    if progress_percent is not None:
        updates.append("progress_percent = ?")
        params.append(progress_percent)
    if remaining_minutes is not None:
        updates.append("remaining_minutes = ?")
        params.append(remaining_minutes)
    if current_layer is not None:
        updates.append("current_layer = ?")
        params.append(current_layer)

    if not updates:
        return

    params.append(print_job_id)

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE print_jobs SET {', '.join(updates)} WHERE id = ?",
                params
            )
            conn.commit()
    except Exception as e:
        log.error(f"Failed to update job progress for {print_job_id}: {e}")


# =============================================================================
# MONITOR COMPATIBILITY FUNCTIONS
# These are called by prusalink_monitor.py and elegoo_monitor.py
# and wrap the core job_started/job_completed/update_job_progress functions.
# =============================================================================

# Per-printer active print_job_id tracking for monitors that don't track it themselves
_active_print_jobs = {}  # printer_id -> print_job_id


def on_print_start(printer_id: int, filename: str, total_layers: int = None,
                   layer_count: int = None, remaining_min: int = None):
    """Called by PrusaLink/Elegoo monitors when a print starts."""
    pj_id = job_started(
        printer_id=printer_id,
        job_name=filename,
        total_layers=total_layers or layer_count,
    )
    if pj_id:
        _active_print_jobs[printer_id] = pj_id


def on_print_complete(printer_id: int, filename: str,
                      duration_seconds: float = None, filament_used_g: float = None):
    """Called by PrusaLink/Elegoo monitors when a print completes."""
    pj_id = _active_print_jobs.pop(printer_id, None)
    if pj_id:
        job_completed(
            printer_id=printer_id,
            print_job_id=pj_id,
            success=True,
            duration_seconds=duration_seconds,
        )


def on_print_failed(printer_id: int, filename: str, reason: str = None):
    """Called by PrusaLink/Elegoo monitors when a print fails."""
    pj_id = _active_print_jobs.pop(printer_id, None)
    if pj_id:
        job_completed(
            printer_id=printer_id,
            print_job_id=pj_id,
            success=False,
            fail_reason=reason,
        )


def on_print_paused(printer_id: int):
    """Called by PrusaLink/Elegoo monitors when a print is paused."""
    log.info(f"Print paused on printer {printer_id}")


def on_progress_update(printer_id: int, progress_percent: float,
                       current_layer: int = None, total_layers: int = None,
                       remaining_min: int = None):
    """Called by PrusaLink/Elegoo monitors for progress updates."""
    pj_id = _active_print_jobs.get(printer_id)
    if pj_id:
        update_job_progress(
            print_job_id=pj_id,
            progress_percent=int(progress_percent) if progress_percent is not None else None,
            remaining_minutes=remaining_min,
            current_layer=current_layer,
        )


# =============================================================================
# ALERT DISPATCH
# =============================================================================

def dispatch_alert(
    alert_type: str,
    severity: str,
    title: str,
    message: str = "",
    printer_id: int = None,
    job_id: int = None,
    spool_id: int = None,
    metadata: dict = None,
):
    """
    Create alert records for all users who have this alert type enabled.
    Uses raw SQL to avoid importing SQLAlchemy into the monitor daemon.
    Handles deduplication for repeated alerts.
    """
    try:
        with get_db(row_factory=sqlite3.Row) as conn:
            cur = conn.cursor()

            # Get all users who have this alert type enabled
            cur.execute("""
                SELECT DISTINCT ap.user_id
                FROM alert_preferences ap
                WHERE UPPER(ap.alert_type) = ? AND ap.in_app = 1
            """, (alert_type.upper(),))
            users = [row['user_id'] for row in cur.fetchall()]

            # If no preferences exist, alert all users (default on)
            if not users:
                cur.execute("SELECT id FROM users")
                users = [row['id'] for row in cur.fetchall()]

            # Check for duplicate (same type, printer, title in last 5 minutes)
            cur.execute("""
                SELECT id FROM alerts
                WHERE alert_type = ?
                  AND printer_id IS ?
                  AND title = ?
                  AND created_at > datetime('now', '-5 minutes')
                LIMIT 1
            """, (alert_type.upper(), printer_id, title))

            if cur.fetchone():
                return  # Duplicate, skip

            # Create alert for each user
            metadata_json = json.dumps(metadata) if metadata else None

            for user_id in users:
                cur.execute("""
                    INSERT INTO alerts (user_id, alert_type, severity, title, message,
                                        printer_id, job_id, spool_id, metadata_json,
                                        is_read, is_dismissed, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, datetime('now'))
                """, (user_id, alert_type.upper(), severity.upper(), title, message,
                      printer_id, job_id, spool_id, metadata_json))

            conn.commit()

        log.debug(f"Dispatched alert '{title}' to {len(users)} users")

        # Push to WebSocket
        ws_push("alert_new", {
            "alert_type": alert_type,
            "severity": severity,
            "title": title,
            "message": message,
            "printer_id": printer_id,
            "job_id": job_id,
            "count": len(users),
        })

    except Exception as e:
        log.error(f"Failed to dispatch alert: {e}")


# =============================================================================
# LOW SPOOL DETECTION (Universal)
# =============================================================================

def check_low_spool(
    printer_id: int,
    slot_number: int,
    remaining_grams: float,
    threshold_grams: float = 100,
):
    """
    Check if spool is low and create alert if needed.
    Works for any printer type that reports filament weight.
    """
    if remaining_grams is None or remaining_grams >= threshold_grams:
        return

    try:
        with get_db(row_factory=sqlite3.Row) as conn:
            cur = conn.cursor()

            # Get printer and slot info
            cur.execute("""
                SELECT p.name, p.nickname, fs.id as slot_id, s.id as spool_id,
                       fl.brand, fl.material, s.color
                FROM printers p
                LEFT JOIN filament_slots fs ON fs.printer_id = p.id AND fs.slot_number = ?
                LEFT JOIN spools s ON s.id = fs.assigned_spool_id
                LEFT JOIN filament_library fl ON fl.id = s.filament_id
                WHERE p.id = ?
            """, (slot_number, printer_id))

            row = cur.fetchone()
            if not row:
                return

            printer_name = row['nickname'] or row['name']
            spool_desc = f"{row['brand'] or ''} {row['material'] or ''} {row['color'] or ''}".strip() or "Unknown"
            spool_id = row['spool_id']

        dispatch_alert(
            alert_type="spool_low",
            severity="warning",
            title=f"Low Filament: {spool_desc}",
            message=f"{remaining_grams:.0f}g remaining on {printer_name} slot {slot_number}",
            printer_id=printer_id,
            spool_id=spool_id,
            metadata={"slot": slot_number, "remaining_g": remaining_grams}
        )

    except Exception as e:
        log.error(f"Failed to check low spool for printer {printer_id}: {e}")
