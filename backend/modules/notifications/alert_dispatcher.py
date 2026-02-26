"""
O.D.I.N. — Alert Dispatcher

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
    from modules.notifications.quiet_hours import should_suppress_notification
except ImportError:
    def should_suppress_notification(org_id=None): return False
import smtplib
import threading
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, List

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.base import AlertType, AlertSeverity
from core.models import SystemConfig
from modules.notifications.models import Alert, AlertPreference, PushSubscription

logger = logging.getLogger("alert_dispatcher")


# ============================================================
# Default preferences for new users
# ============================================================

DEFAULT_PREFERENCES = [
    {"alert_type": AlertType.PRINT_COMPLETE, "in_app": True, "browser_push": False, "email": False, "threshold_value": None},
    {"alert_type": AlertType.PRINT_FAILED, "in_app": True, "browser_push": True, "email": False, "threshold_value": None},
    {"alert_type": AlertType.SPOOL_LOW, "in_app": True, "browser_push": False, "email": False, "threshold_value": 100.0},
    {"alert_type": AlertType.MAINTENANCE_OVERDUE, "in_app": True, "browser_push": False, "email": False, "threshold_value": None},
    {"alert_type": AlertType.JOB_SUBMITTED, "in_app": True, "browser_push": True, "email": False, "threshold_value": None},
    {"alert_type": AlertType.JOB_APPROVED, "in_app": True, "browser_push": False, "email": False, "threshold_value": None},
    {"alert_type": AlertType.JOB_REJECTED, "in_app": True, "browser_push": True, "email": False, "threshold_value": None},
    {"alert_type": AlertType.SPAGHETTI_DETECTED, "in_app": True, "browser_push": True, "email": False, "threshold_value": None},
    {"alert_type": AlertType.FIRST_LAYER_ISSUE, "in_app": True, "browser_push": True, "email": False, "threshold_value": None},
    {"alert_type": AlertType.DETACHMENT_DETECTED, "in_app": True, "browser_push": True, "email": False, "threshold_value": None},
    {"alert_type": AlertType.BED_COOLED, "in_app": True, "browser_push": False, "email": False, "threshold_value": 40.0},
    {"alert_type": AlertType.QUEUE_ADDED, "in_app": False, "browser_push": False, "email": False, "threshold_value": None},
    {"alert_type": AlertType.QUEUE_SKIPPED, "in_app": True, "browser_push": False, "email": False, "threshold_value": None},
    {"alert_type": AlertType.QUEUE_FAILED_START, "in_app": True, "browser_push": True, "email": False, "threshold_value": None},
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
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
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
    # Decrypt password — migration-safe: crypto.decrypt() falls back to raw on failure
    if smtp.get("password"):
        try:
            import core.crypto as crypto
            smtp = dict(smtp)  # copy to avoid mutating the cached ORM value
            smtp["password"] = crypto.decrypt(smtp["password"])
        except Exception:
            pass
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
    
    subject = f"[O.D.I.N.] {emoji} {title}"
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
# Group / Role helpers for targeted dispatch
# ============================================================

def get_group_owner_id(db: Session, user_id: int) -> Optional[int]:
    """Get the group owner for a user. Returns None if user has no group or group has no owner."""
    row = db.execute(
        text("""
            SELECT g.owner_id FROM groups g
            JOIN users u ON u.group_id = g.id
            WHERE u.id = :user_id AND g.owner_id IS NOT NULL
        """),
        {"user_id": user_id}
    ).fetchone()
    return row[0] if row else None


def get_operator_admin_ids(db: Session) -> List[int]:
    """Get all active operator/admin user IDs (fallback when no group owner)."""
    rows = db.execute(
        text("SELECT id FROM users WHERE role IN ('operator', 'admin') AND is_active = 1")
    ).fetchall()
    return [r[0] for r in rows]


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
    metadata: dict = None,
    target_user_ids: List[int] = None,
):
    """
    Fan out an alert to users based on their preferences.

    If target_user_ids is set, only those users receive the alert.
    Otherwise broadcasts to all users (original behavior).

    Handles dedup, creates in-app records, sends push + email.
    """
    # Resolve printer's org for org-level quiet hours + webhook
    _printer_org_id = None
    if printer_id:
        try:
            row = db.execute(text("SELECT org_id FROM printers WHERE id = :id"), {"id": printer_id}).fetchone()
            if row and row.org_id:
                _printer_org_id = row.org_id
        except Exception:
            pass

    # Quiet hours: save alert to DB but suppress external notifications
    _suppress_external = should_suppress_notification(org_id=_printer_org_id)

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

    # Filter to targeted users if specified
    if target_user_ids is not None:
        target_set = set(target_user_ids)
        preferences = [p for p in preferences if p.user_id in target_set]
    
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

    # Org-level webhook dispatch
    if _printer_org_id:
        try:
            from modules.organizations.routes import _get_org_settings
            org_settings = _get_org_settings(db, _printer_org_id)
            org_webhook_url = org_settings.get("webhook_url")
            if org_webhook_url:
                _send_org_webhook(
                    org_webhook_url, org_settings.get("webhook_type", "generic"),
                    alert_type.value, title, message, severity.value
                )
        except Exception as e:
            logger.error(f"Org webhook dispatch error: {e}")

    logger.info(f"Dispatched {alert_type.value} alert to {alerts_created} users: {title}")
    return alerts_created


def _send_org_webhook(url: str, wtype: str, alert_type_value: str,
                      title: str, message: str, severity: str):
    """Send alert to an org's configured webhook URL in a background thread."""
    import httpx

    severity_colors = {"critical": 0xef4444, "warning": 0xf59e0b, "info": 0x3b82f6}
    severity_emoji = {"critical": "\U0001f534", "warning": "\U0001f7e1", "info": "\U0001f535"}
    emoji = severity_emoji.get(severity, "\U0001f535")
    color = severity_colors.get(severity, 0x3b82f6)

    def _send():
        try:
            if wtype == "discord":
                httpx.post(url, json={
                    "embeds": [{"title": f"{emoji} {title}", "description": message or "",
                                "color": color, "footer": {"text": "O.D.I.N."}}]
                }, timeout=10)
            elif wtype == "slack":
                httpx.post(url, json={
                    "blocks": [
                        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {title}"}},
                        {"type": "section", "text": {"type": "mrkdwn", "text": message or ""}}
                    ]
                }, timeout=10)
            elif wtype == "ntfy":
                priority_map = {"critical": "urgent", "warning": "high", "info": "default"}
                httpx.post(url, content=message or title, headers={
                    "Title": title, "Priority": priority_map.get(severity, "default"), "Tags": "printer",
                }, timeout=10)
            elif wtype == "telegram":
                if "|" in url:
                    bot_token, chat_id = url.split("|", 1)
                    api_url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
                else:
                    api_url = f"https://api.telegram.org/bot{url.strip()}/sendMessage"
                    chat_id = ""
                if chat_id:
                    httpx.post(api_url, json={
                        "chat_id": chat_id.strip(), "text": f"{emoji} *{title}*\n{message or ''}",
                        "parse_mode": "Markdown"
                    }, timeout=10)
            else:
                httpx.post(url, json={
                    "event": alert_type_value, "title": title,
                    "message": message or "", "severity": severity
                }, timeout=10)
        except Exception as e:
            logger.error(f"Org webhook dispatch failed ({wtype}): {e}")

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
