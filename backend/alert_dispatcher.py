"""
Alert Dispatcher for PrintFarm Scheduler (v0.17.0)

Central fan-out module that receives events and delivers alerts
to users via their configured channels (in-app, browser push, email).

Usage:
    from alert_dispatcher import dispatch_alert
    
    dispatch_alert(
        db=db,
        alert_type=AlertType.PRINT_FAILED,
        severity=AlertSeverity.CRITICAL,
        title="Print Failed: Baby Yoda (X1C)",
        message="Job #142 failed on X1C at 67% progress.",
        printer_id=1,
        job_id=142
    )
"""

import json
import logging

try:
    from quiet_hours import should_suppress_notification
except ImportError:
    def should_suppress_notification(): return False
import smtplib
import threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import (
    Alert, AlertPreference, AlertType, AlertSeverity,
    PushSubscription, SystemConfig
)

logger = logging.getLogger("alert_dispatcher")


# ============================================================
# Default preferences for new users
# ============================================================

DEFAULT_PREFERENCES = [
    {"alert_type": AlertType.PRINT_COMPLETE, "in_app": True, "browser_push": False, "email": False, "threshold_value": None},
    {"alert_type": AlertType.PRINT_FAILED, "in_app": True, "browser_push": True, "email": False, "threshold_value": None},
    {"alert_type": AlertType.SPOOL_LOW, "in_app": True, "browser_push": False, "email": False, "threshold_value": 100.0},
    {"alert_type": AlertType.MAINTENANCE_OVERDUE, "in_app": True, "browser_push": False, "email": False, "threshold_value": None},
]


def seed_alert_preferences(db: Session, user_id: int):
    """Create default alert preferences for a new user."""
    for pref in DEFAULT_PREFERENCES:
        existing = db.query(AlertPreference).filter(
            AlertPreference.user_id == user_id,
            AlertPreference.alert_type == pref["alert_type"]
        ).first()
        if not existing:
            db.add(AlertPreference(user_id=user_id, **pref))
    db.commit()


# ============================================================
# Deduplication
# ============================================================

def _should_deduplicate(db, user_id, alert_type, printer_id, spool_id, job_id):
    """
    Check if we should skip creating this alert.
    
    - spool_low: Skip if unread alert exists for same spool
    - maintenance_overdue: Skip if unread alert exists for same printer within 24h
    - print events: Never deduplicate
    """
    if alert_type == AlertType.SPOOL_LOW and spool_id:
        existing = db.query(Alert).filter(
            Alert.user_id == user_id,
            Alert.alert_type == AlertType.SPOOL_LOW,
            Alert.spool_id == spool_id,
            Alert.is_read == False,
            Alert.is_dismissed == False
        ).first()
        return existing is not None
    
    if alert_type == AlertType.MAINTENANCE_OVERDUE and printer_id:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        existing = db.query(Alert).filter(
            Alert.user_id == user_id,
            Alert.alert_type == AlertType.MAINTENANCE_OVERDUE,
            Alert.printer_id == printer_id,
            Alert.is_read == False,
            Alert.created_at > cutoff
        ).first()
        return existing is not None
    
    return False


# ============================================================
# Delivery: In-App
# ============================================================

def _deliver_in_app(db, user_id, alert_type, severity, title, message,
                    printer_id, job_id, spool_id, metadata):
    """Create an alert record in the database."""
    alert = Alert(
        user_id=user_id,
        alert_type=alert_type,
        severity=severity,
        title=title,
        message=message,
        printer_id=printer_id,
        job_id=job_id,
        spool_id=spool_id,
        metadata_json=metadata
    )
    db.add(alert)
    return alert


# ============================================================
# Delivery: Browser Push
# ============================================================

def _deliver_browser_push(db, user_id, title, message, severity):
    """Send browser push notification to all of a user's subscriptions."""
    try:
        from pywebpush import webpush
    except ImportError:
        logger.warning("pywebpush not installed — skipping browser push")
        return
    
    import os
    vapid_private_key = os.environ.get("VAPID_PRIVATE_KEY")
    vapid_email = os.environ.get("VAPID_EMAIL", "mailto:admin@example.com")
    
    if not vapid_private_key:
        logger.warning("VAPID_PRIVATE_KEY not set — skipping browser push")
        return
    
    subscriptions = db.query(PushSubscription).filter(
        PushSubscription.user_id == user_id
    ).all()
    
    payload = json.dumps({
        "title": title,
        "body": message or "",
        "severity": severity,
        "url": "/alerts"
    })
    
    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh_key, "auth": sub.auth_key}
                },
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims={"sub": vapid_email}
            )
        except Exception as e:
            logger.error(f"Push failed for subscription {sub.id}: {e}")
            if "410" in str(e) or "404" in str(e):
                db.delete(sub)
                logger.info(f"Removed expired push subscription {sub.id}")


# ============================================================
# Delivery: SMTP Email
# ============================================================

def _get_smtp_config(db):
    """Get SMTP configuration from system_config."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    if not config or not config.value:
        return None
    smtp = config.value
    if not smtp.get("enabled") or not smtp.get("host"):
        return None
    return smtp


def _deliver_email(db, user_id, title, message, severity):
    """Send email notification in a background thread."""
    smtp_config = _get_smtp_config(db)
    if not smtp_config:
        return
    
    user = db.execute(
        text("SELECT email FROM users WHERE id = :id"),
        {"id": user_id}
    ).fetchone()
    
    if not user or not user.email:
        return
    
    emoji_map = {"critical": "\U0001f534", "warning": "\U0001f7e1", "info": "\U0001f7e2"}
    emoji = emoji_map.get(severity, "")
    
    subject = f"[PrintFarm] {emoji} {title}"
    body = f"""{title}

{message or ''}

---
You're receiving this because you enabled email alerts.
Manage preferences in Settings > Notifications.
"""
    
    user_email = user.email
    
    def send():
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = smtp_config["from_address"]
            msg["To"] = user_email
            msg.attach(MIMEText(body, "plain"))
            
            if smtp_config.get("use_tls", True):
                server = smtplib.SMTP(smtp_config["host"], smtp_config.get("port", 587))
                server.starttls()
            else:
                server = smtplib.SMTP(smtp_config["host"], smtp_config.get("port", 25))
            
            if smtp_config.get("username") and smtp_config.get("password"):
                server.login(smtp_config["username"], smtp_config["password"])
            
            server.send_message(msg)
            server.quit()
            logger.info(f"Email sent to {user_email}: {title}")
        except Exception as e:
            logger.error(f"Failed to send email to {user_email}: {e}")
    
    thread = threading.Thread(target=send, daemon=True)
    thread.start()


# ============================================================
# Main Dispatcher
# ============================================================

def dispatch_alert(

    db: Session,
    alert_type: AlertType,
    severity: AlertSeverity,
    title: str,
    message: str = "",
    printer_id: int = None,
    job_id: int = None,
    spool_id: int = None,
    metadata: dict = None
):
    """
    Fan out an alert to all users based on their preferences.
    
    Handles dedup, creates in-app records, sends push + email.
    """
    # Quiet hours: save alert to DB but suppress external notifications
    _suppress_external = should_suppress_notification()

    preferences = db.query(AlertPreference).filter(
        AlertPreference.alert_type == alert_type
    ).all()
    
    # Auto-seed preferences for existing users if none found
    if not preferences:
        users = db.execute(text("SELECT id FROM users WHERE is_active = 1")).fetchall()
        for user_row in users:
            seed_alert_preferences(db, user_row.id)
        preferences = db.query(AlertPreference).filter(
            AlertPreference.alert_type == alert_type
        ).all()
    
    alerts_created = 0
    
    for pref in preferences:
        if _should_deduplicate(db, pref.user_id, alert_type, printer_id, spool_id, job_id):
            continue
        
        if pref.in_app:
            _deliver_in_app(
                db, pref.user_id, alert_type, severity,
                title, message, printer_id, job_id, spool_id, metadata
            )
            alerts_created += 1
        
        if pref.browser_push and not _suppress_external:
            _deliver_browser_push(db, pref.user_id, title, message, severity.value)

        if pref.email and not _suppress_external:
            _deliver_email(db, pref.user_id, title, message, severity.value)
    
    if alerts_created > 0:
        db.commit()
    
    # Dispatch to webhooks (ntfy, telegram, discord, slack)
    try:
        from main import _dispatch_to_webhooks
        _dispatch_to_webhooks(db, alert_type.value, title, message, severity.value)
    except ImportError:
        pass
    except Exception as e:
        logger.error(f"Webhook dispatch error: {e}")
    
    logger.info(f"Dispatched {alert_type.value} alert to {alerts_created} users: {title}")
    return alerts_created
