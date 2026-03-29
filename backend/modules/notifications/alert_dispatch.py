"""
Alert dispatch and low-spool / bed-cooled monitoring.

Provides dispatch_alert(), check_low_spool(), _start_bed_cooled_monitor().
Central alert creation for all notification channels.
"""

import json
import logging
import threading
from typing import Optional

from sqlalchemy import text

from core.db import engine
from core.db_compat import sql
from core.event_bus import get_event_bus
from core.interfaces.event_bus import Event

log = logging.getLogger("printer_events")

# Per-printer bed-cooled monitor cancel flags
_bed_cooled_monitors = {}  # printer_id -> threading.Event (cancel flag)


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
    Create alert records and deliver to all notification channels.

    Uses raw SQL to avoid importing SQLAlchemy — safe for monitor daemons.
    Handles deduplication, in-app alerts, webhooks, push, and email.
    Respects quiet hours for external channels.
    """
    # Track per-user channel preferences for external delivery after commit
    _push_users = []  # user_ids with browser_push enabled
    _email_users = []  # user_ids with email enabled

    try:
        with engine.begin() as conn:
            # Get all users with preferences for this alert type
            pref_rows = conn.execute(text("""
                SELECT DISTINCT ap.user_id, ap.in_app, ap.browser_push, ap.email
                FROM alert_preferences ap
                WHERE UPPER(ap.alert_type) = :atype
                  AND (ap.in_app = 1 OR ap.browser_push = 1 OR ap.email = 1)
            """), {"atype": alert_type.upper()}).mappings().fetchall()

            in_app_users = [r['user_id'] for r in pref_rows if r['in_app']]
            _push_users = [r['user_id'] for r in pref_rows if r['browser_push']]
            _email_users = [r['user_id'] for r in pref_rows if r['email']]

            # If no preferences exist, alert all users in-app (default on)
            if not pref_rows:
                all_users = conn.execute(text("SELECT id FROM users")).mappings().fetchall()
                in_app_users = [row['id'] for row in all_users]

            # Check for duplicate (same type, printer, title in last 5 minutes)
            dup = conn.execute(text(f"""
                SELECT id FROM alerts
                WHERE alert_type = :atype
                  AND printer_id IS :pid
                  AND title = :title
                  AND created_at > {sql.now_offset('-5 minutes')}
                LIMIT 1
            """), {"atype": alert_type.lower(), "pid": printer_id, "title": title}).fetchone()

            if dup:
                return  # Duplicate, skip

            # Create in-app alert for each user
            metadata_json = json.dumps(metadata) if metadata else None

            for user_id in in_app_users:
                conn.execute(text(f"""
                    INSERT INTO alerts (user_id, alert_type, severity, title, message,
                                        printer_id, job_id, spool_id, metadata_json,
                                        is_read, is_dismissed, created_at)
                    VALUES (:uid, :atype, :sev, :title, :msg, :pid, :jid, :sid, :meta, 0, 0, {sql.now()})
                """), {"uid": user_id, "atype": alert_type.lower(), "sev": severity.lower(),
                       "title": title, "msg": message, "pid": printer_id, "jid": job_id,
                       "sid": spool_id, "meta": metadata_json})

        log.debug(f"Dispatched alert '{title}' to {len(in_app_users)} users")

        # Publish alert event so WebSocket hub and mqtt_republish subscribers can react
        get_event_bus().publish(Event(
            event_type="notifications.alert_dispatched",
            source_module="notifications",
            data={
                "alert_type": alert_type,
                "severity": severity,
                "title": title,
                "message": message,
                "printer_id": printer_id,
                "job_id": job_id,
                "count": len(in_app_users),
            },
        ))

        # --- External channel delivery (webhooks, push, email) ---
        try:
            from modules.notifications.quiet_hours import should_suppress_notification
            suppress = should_suppress_notification()
        except Exception:
            suppress = False

        if not suppress:
            # Webhooks (system-wide, not per-user)
            try:
                from modules.notifications.channels import send_webhook
                send_webhook(alert_type, title, message, severity,
                             printer_id=printer_id, job_id=job_id)
            except Exception as e:
                log.debug(f"Webhook delivery failed: {e}")

            # Per-user push notifications
            if _push_users:
                try:
                    from modules.notifications.channels import send_push_notification
                    for uid in _push_users:
                        try:
                            send_push_notification(uid, alert_type, title, message,
                                                   printer_id=printer_id, job_id=job_id)
                        except Exception as e:
                            log.debug(f"Push delivery failed for user {uid}: {e}")
                except ImportError:
                    pass

            # Per-user email
            if _email_users:
                try:
                    from modules.notifications.channels import send_email
                    for uid in _email_users:
                        try:
                            send_email(uid, alert_type, title, message,
                                       printer_id=printer_id, job_id=job_id)
                        except Exception as e:
                            log.debug(f"Email delivery failed for user {uid}: {e}")
                except ImportError:
                    pass

    except Exception as e:
        log.error(f"Failed to dispatch alert: {e}")


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
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT p.name, p.nickname, fs.id as slot_id, s.id as spool_id,
                       fl.brand, fl.material, s.color
                FROM printers p
                LEFT JOIN filament_slots fs ON fs.printer_id = p.id AND fs.slot_number = :sn
                LEFT JOIN spools s ON s.id = fs.assigned_spool_id
                LEFT JOIN filament_library fl ON fl.id = s.filament_id
                WHERE p.id = :pid
            """), {"sn": slot_number, "pid": printer_id}).mappings().fetchone()

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


def _start_bed_cooled_monitor(printer_id: int, printer_name: str):
    """Start background thread to monitor bed temp after print completion.

    Checks every 30 seconds until bed temp drops below the configured
    threshold (default 40°C), then dispatches a bed_cooled alert.
    Times out after 2 hours.
    """
    # Cancel any existing monitor for this printer
    cancel = _bed_cooled_monitors.pop(printer_id, None)
    if cancel:
        cancel.set()

    stop_event = threading.Event()
    _bed_cooled_monitors[printer_id] = stop_event

    def _monitor():
        import time
        threshold = 40
        max_checks = 240  # 2 hours at 30s intervals

        # Read threshold from alert preferences if available
        try:
            with engine.connect() as conn:
                row = conn.execute(text("""
                    SELECT threshold_value FROM alert_preferences
                    WHERE UPPER(alert_type) = 'BED_COOLED' AND threshold_value IS NOT NULL
                    LIMIT 1
                """)).mappings().fetchone()
                if row and row['threshold_value']:
                    threshold = float(row['threshold_value'])
        except Exception as e:
            log.debug(f"Failed to read bed_cooled threshold: {e}")

        for _ in range(max_checks):
            if stop_event.is_set():
                return
            time.sleep(30)
            if stop_event.is_set():
                return

            try:
                with engine.connect() as conn:
                    row = conn.execute(
                        text("SELECT bed_temp FROM printers WHERE id = :pid"), {"pid": printer_id}
                    ).mappings().fetchone()
                    if row and row['bed_temp'] is not None:
                        bed_temp = float(row['bed_temp'])
                        if bed_temp < threshold:
                            dispatch_alert(
                                alert_type="bed_cooled",
                                severity="info",
                                title=f"Bed Cooled: {printer_name}",
                                message=f"Bed temperature dropped to {bed_temp:.0f}°C — safe to remove print",
                                printer_id=printer_id,
                            )
                            _bed_cooled_monitors.pop(printer_id, None)
                            return
            except Exception as e:
                log.debug(f"Bed cooled check failed for printer {printer_id}: {e}")

        # Timeout - clean up
        _bed_cooled_monitors.pop(printer_id, None)
        log.debug(f"Bed cooled monitor timed out for printer {printer_id}")

    t = threading.Thread(target=_monitor, daemon=True, name=f"bed-cool-{printer_id}")
    t.start()
