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
from dataclasses import dataclass

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
from core.rbac import check_org_access
from core.webhook_utils import safe_post, trusted_post, WebhookSSRFError
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


def seed_alert_preferences(db: Session, user_id: int, *, commit: bool = True):
    """Create default alert preferences for a new user."""
    for pref in DEFAULT_PREFERENCES:
        existing = db.query(AlertPreference).filter(
            AlertPreference.user_id == user_id,
            AlertPreference.alert_type == pref["alert_type"]
        ).first()
        if not existing:
            db.add(AlertPreference(user_id=user_id, **pref))
    if commit:
        db.commit()


# ============================================================
# Deduplication
# ============================================================

def lock_queue_alert(db: Session, job_id: int, alert_type: AlertType) -> bool:
    """Serialize queue outcome dedup within the caller-owned transaction."""
    if db.get_bind().dialect.name == "postgresql":
        return db.execute(text("SELECT id FROM jobs WHERE id=:id FOR UPDATE"), {"id": job_id}).first() is not None
    # Even a zero-row update obtains SQLite's writer boundary. Busy must propagate.
    db.execute(text("UPDATE alerts SET is_read=is_read WHERE job_id=:id AND alert_type=:type"),
               {"id": job_id, "type": alert_type.value})
    return db.execute(text("SELECT id FROM jobs WHERE id=:id"), {"id": job_id}).first() is not None


def _should_deduplicate(db, user_id, alert_type, printer_id, spool_id, job_id, metadata=None):
    """
    Check if we should skip creating this alert.
    
    - spool_low: Skip if unread alert exists for same spool
    - maintenance_overdue: Skip if unread alert exists for same printer within 24h
    - print events: Never deduplicate
    """
    if alert_type in (AlertType.QUEUE_SKIPPED, AlertType.QUEUE_FAILED_START) and job_id:
        reason = (metadata or {}).get("reason")
        existing = db.query(Alert).filter(Alert.user_id == user_id,
            Alert.alert_type == alert_type, Alert.job_id == job_id,
            Alert.is_read.is_(False), Alert.is_dismissed.is_(False)).all()
        for alert in existing:
            stored = alert.metadata_json or {}
            if isinstance(stored, str):
                try:
                    stored = json.loads(stored)
                except ValueError:
                    continue
            if isinstance(stored, dict) and stored.get("reason") == reason:
                return True
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
    # v1.8.9 (codex pass 19): ITAR mode hard-disables web push here
    # too. The `channels.py` send_push_notification path is gated by
    # Fix 44; this legacy dispatcher path was missed in that pass.
    # Browser push endpoints live on vendor infrastructure
    # (fcm.googleapis.com, etc.) and pywebpush's transport cannot be
    # DNS-pinned through — fail closed.
    from core.itar import is_itar_mode
    if is_itar_mode():
        logger.info(
            "Browser push skipped entirely under ITAR (user %s)", user_id,
        )
        return

    import os
    # Sign with the effective pair whose public half /push/vapid-key serves:
    # the VAPID_PRIVATE_KEY environment override when set, else the stored pair.
    from modules.notifications.vapid import read_vapid_keys
    effective = read_vapid_keys(db)
    vapid_private_key = effective["private_key"] if effective else None
    vapid_email = os.environ.get("VAPID_EMAIL", "mailto:admin@example.com")

    # Avoid serializing a cold burst of job notifications behind pywebpush's
    # import lock when push is intentionally unconfigured (the common local
    # and self-hosted EDU case). Configuration is authoritative here.
    if not vapid_private_key:
        logger.debug("No VAPID key pair stored or configured — skipping browser push")
        return

    try:
        from pywebpush import webpush
    except ImportError:
        logger.warning("pywebpush not installed — skipping browser push")
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
        except Exception as e:
            logger.debug(f"Failed to decrypt SMTP password (using raw): {e}")
    return smtp


def _deliver_email(db, user_id, title, message, severity):
    """Send email notification in a background thread (Pro: email_notifications)."""
    from core.license_manager import get_license
    if not get_license().has_feature("email_notifications"):
        return
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

            # v1.8.9 (codex pass 7): runtime ITAR guard on every SMTP
            # connect — boot-audit alone misses DNS drift.
            from core.itar import enforce_host_destination
            enforce_host_destination(smtp_config["host"], scheme="smtp")

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


def get_job_approval_reviewer_ids(db: Session, submitter: dict) -> List[int]:
    """Who is told that a submitter's job awaits approval.

    The submitter's group owner when the owner can open the group's jobs
    (a member of the group, or a superadmin). Otherwise — no owner, or an
    owner outside the group who could neither see nor approve the job — the
    fallback reviewers: admins and operators of the submitter's group.
    """
    group_id = submitter.get("group_id")
    owner_id = get_group_owner_id(db, submitter["id"])
    if owner_id:
        owner = db.execute(
            text("SELECT id, role, group_id FROM users WHERE id = :id AND is_active IS TRUE"),
            {"id": owner_id},
        ).mappings().first()
        if owner and check_org_access(dict(owner), group_id):
            return [owner_id]
        logger.warning(
            "Group owner %s cannot access group %s; approval alert falls back to group reviewers",
            owner_id, group_id,
        )
    return get_operator_admin_ids(db, group_id)


def get_operator_admin_ids(db: Session, group_id: int | None = None) -> List[int]:
    """Get eligible fallback reviewers without crossing operator tenants."""
    rows = db.execute(
        text(
            "SELECT id FROM users "
            "WHERE role IN ('operator', 'admin') AND is_active IS TRUE "
            "AND (:group_id IS NULL OR role = 'admin' OR group_id = :group_id)"
        ),
        {"group_id": group_id},
    ).fetchall()
    return [r[0] for r in rows]


# ============================================================
# Main Dispatcher
# ============================================================

def stage_alert(
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
    """Stage tenant-scoped in-app alerts and immutable delivery intent without committing."""
    from modules.notifications.recipient_policy import resolve_alert_scope
    if alert_type in (AlertType.QUEUE_SKIPPED, AlertType.QUEUE_FAILED_START) and job_id:
        if not lock_queue_alert(db, job_id, alert_type):
            return None
        db.flush()
    try:
        eligible_users, _printer_org_id = resolve_alert_scope(
            db, printer_id=printer_id, job_id=job_id, spool_id=spool_id)
    except Exception:
        logger.warning("Alert resource scope could not be resolved; staging refused")
        raise

    # Quiet hours: save alert to DB but suppress external notifications
    _suppress_external = should_suppress_notification(org_id=_printer_org_id, db=db)

    preferences = db.query(AlertPreference).filter(
        AlertPreference.alert_type == alert_type
    ).all()

    # Auto-seed preferences without committing the caller transaction.
    seeded_preferences = False
    if not preferences:
        users = db.execute(text("SELECT id FROM users WHERE is_active IS TRUE")).fetchall()
        for user_row in users:
            seed_alert_preferences(db, user_row.id, commit=False)
        db.flush()
        seeded_preferences = True
        preferences = db.query(AlertPreference).filter(
            AlertPreference.alert_type == alert_type
        ).all()

    preferences = [p for p in preferences if p.user_id in eligible_users]
    # Filter to targeted users if specified
    if target_user_ids is not None:
        target_set = set(target_user_ids)
        preferences = [p for p in preferences if p.user_id in target_set]
    
    alerts_created = 0
    deduplicated = 0
    in_app_users = []
    push_users = []
    email_users = []
    for pref in preferences:
        if _should_deduplicate(db, pref.user_id, alert_type, printer_id, spool_id, job_id, metadata):
            deduplicated += 1
            continue
        if pref.in_app:
            _deliver_in_app(db, pref.user_id, alert_type, severity, title, message,
                            printer_id, job_id, spool_id, metadata)
            alerts_created += 1
            in_app_users.append(pref.user_id)
        if pref.browser_push:
            push_users.append(pref.user_id)
        if pref.email:
            email_users.append(pref.user_id)
    deferred_recorded = 0
    if _suppress_external and not (bool(preferences) and deduplicated == len(preferences)):
        # Quiet hours: record what was suppressed so the digest of the scope that
        # caused it delivers it once (same transaction): external channels of
        # recipients without an in-app row, and one webhook row per alert.
        deferred = {}
        for uid in email_users:
            if uid not in in_app_users:
                deferred.setdefault(uid, set()).add("email")
        for uid in push_users:
            if uid not in in_app_users:
                deferred.setdefault(uid, set()).add("push")
        from modules.notifications.deferred_deliveries import record_suppressed, webhook_destination_exists
        from modules.notifications.quiet_hours import governing_scope
        atype = alert_type.value if hasattr(alert_type, "value") else alert_type
        deferred_recorded = record_suppressed(
            db, org_id=_printer_org_id, scope=governing_scope(_printer_org_id, db), recipients=deferred,
            webhook=webhook_destination_exists(db, _printer_org_id, atype),
            alert_type=atype, severity=severity.value if hasattr(severity, "value") else severity,
            title=title, message=message, printer_id=printer_id, job_id=job_id, spool_id=spool_id)
    return StagedAlert(alerts_created, seeded_preferences or alerts_created > 0 or deferred_recorded > 0 or
                       (job_id is not None and alert_type in (AlertType.QUEUE_SKIPPED, AlertType.QUEUE_FAILED_START)), alert_type, severity, title, message,
                       printer_id, job_id, spool_id, _printer_org_id,
                       _suppress_external or (bool(preferences) and deduplicated == len(preferences)), tuple(push_users), tuple(email_users), tuple(in_app_users))


@dataclass(frozen=True, repr=False)
class StagedAlert:
    alerts_created: int
    commit_required: bool
    alert_type: AlertType
    severity: AlertSeverity
    title: str
    message: str
    printer_id: Optional[int]
    job_id: Optional[int]
    spool_id: Optional[int]
    org_id: Optional[int]
    suppress_external: bool
    push_users: tuple[int, ...]
    email_users: tuple[int, ...]
    in_app_users: tuple[int, ...]


def deliver_staged_alert(db: Session, plan: Optional[StagedAlert]):
    """Best-effort external delivery. Caller must have confirmed its commit first."""
    if plan is None:
        return
    from core.dispatch_connection import dispatch_scope
    # One connection for this dispatch's after-commit work (core.dispatch_connection).
    with dispatch_scope(db.get_bind()):
        _deliver_staged_alert(db, plan)


def _deliver_staged_alert(db: Session, plan: StagedAlert):
    from core.dispatch_connection import dispatch_transaction
    from modules.notifications.recipient_policy import resolve_alert_scope
    try:
        with dispatch_transaction(db.get_bind()) as scope_connection:
            eligible, org_id = resolve_alert_scope(scope_connection, printer_id=plan.printer_id,
                job_id=plan.job_id, spool_id=plan.spool_id)
        if org_id != plan.org_id:
            return
        recipients = sorted(set(plan.in_app_users) & eligible)
        if recipients:
            from core.event_bus import get_event_bus
            from core.interfaces.event_bus import Event
            try:
                get_event_bus().publish(Event("notifications.alert_dispatched", "notifications", {
                    "alert_type": plan.alert_type.value, "severity": plan.severity.value,
                    "title": plan.title, "message": plan.message,
                    "printer_id": plan.printer_id, "job_id": plan.job_id, "spool_id": plan.spool_id,
                    "count": len(recipients), "_ws_user_ids": recipients,
                }))
            except Exception:
                logger.warning("Committed alert event publication failed")
        if plan.suppress_external:
            return
        for uid in plan.push_users:
            if uid in eligible:
                # Push may remove expired subscriptions. Keep that cleanup in
                # its own transaction, separate from the committed job session.
                try:
                    with dispatch_transaction(db.get_bind()) as push_connection, Session(bind=push_connection) as push_db:
                        _deliver_browser_push(push_db, uid, plan.title, plan.message, plan.severity.value)
                        if push_db.deleted:
                            # Joins the dispatch transaction; a failure rolls the cleanup back.
                            push_db.commit()
                except Exception:
                    logger.warning("Postcommit push delivery or cleanup failed")
        for uid in plan.email_users:
            if uid in eligible:
                _deliver_email(db, uid, plan.title, plan.message, plan.severity.value)
        if org_id is None:
            from modules.notifications.channels import send_webhook
            send_webhook(plan.alert_type.value, plan.title, plan.message, plan.severity.value,
                         printer_id=plan.printer_id, job_id=plan.job_id)
        else:
            from core.registry import registry
            provider = registry.get_provider("OrgSettingsProvider")
            with dispatch_transaction(db.get_bind()) as settings_connection:
                settings = provider.get_org_settings(settings_connection, org_id) if provider else {}
            if settings.get("webhook_url"):
                _send_org_webhook(settings["webhook_url"], settings.get("webhook_type", "generic"),
                    plan.alert_type.value, plan.title, plan.message, plan.severity.value)
    except Exception:
        logger.warning("External alert delivery failed after confirmed commit")


def dispatch_alert(db: Session, alert_type: AlertType, severity: AlertSeverity,
                   title: str, message: str = "", printer_id: int = None,
                   job_id: int = None, spool_id: int = None, metadata: dict = None,
                   target_user_ids: List[int] = None):
    try:
        plan = stage_alert(db, alert_type, severity, title, message, printer_id,
                           job_id, spool_id, metadata, target_user_ids)
    except ValueError:
        # Legacy callers refuse invalid resource references without delivery.
        return 0
    if plan is None:
        return 0
    if plan.commit_required or db.new or db.dirty or db.deleted:
        db.commit()
    deliver_staged_alert(db, plan)
    return plan.alerts_created


def _send_org_webhook(url: str, wtype: str, alert_type_value: str,
                      title: str, message: str, severity: str):
    """Send alert to an org's configured webhook URL in a background thread.

    R8 (2026-04-12): routes through safe_post() so DNS resolution and
    private-address checks happen at dispatch time, defeating DNS rebinding
    and split-horizon DNS on user-configured org webhooks.
    """
    severity_colors = {"critical": 0xef4444, "warning": 0xf59e0b, "info": 0x3b82f6}
    severity_emoji = {"critical": "\U0001f534", "warning": "\U0001f7e1", "info": "\U0001f535"}
    emoji = severity_emoji.get(severity, "\U0001f535")
    color = severity_colors.get(severity, 0x3b82f6)

    def _send():
        try:
            if wtype == "discord":
                safe_post(url, json={
                    "embeds": [{"title": f"{emoji} {title}", "description": message or "",
                                "color": color, "footer": {"text": "O.D.I.N."}}]
                }, timeout=10)
            elif wtype == "slack":
                safe_post(url, json={
                    "blocks": [
                        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {title}"}},
                        {"type": "section", "text": {"type": "mrkdwn", "text": message or ""}}
                    ]
                }, timeout=10)
            elif wtype == "ntfy":
                priority_map = {"critical": "urgent", "warning": "high", "info": "default"}
                safe_post(url, content=message or title, headers={
                    "Title": title, "Priority": priority_map.get(severity, "default"), "Tags": "printer",
                }, timeout=10)
            elif wtype == "telegram":
                # Hardcoded Telegram bot API — trusted_post for HTTP_PROXY.
                if "|" in url:
                    bot_token, chat_id = url.split("|", 1)
                    api_url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
                else:
                    api_url = f"https://api.telegram.org/bot{url.strip()}/sendMessage"
                    chat_id = ""
                if chat_id:
                    trusted_post(api_url, json={
                        "chat_id": chat_id.strip(), "text": f"{emoji} *{title}*\n{message or ''}",
                        "parse_mode": "Markdown"
                    }, timeout=10)
            else:
                safe_post(url, json={
                    "event": alert_type_value, "title": title,
                    "message": message or "", "severity": severity
                }, timeout=10)
        except WebhookSSRFError as e:
            from core.redaction import redact_urls
            logger.warning(f"Org webhook SSRF-blocked ({wtype}): {redact_urls(e)}")
        except Exception as e:
            from core.redaction import redact_urls
            logger.error(f"Org webhook dispatch failed ({wtype}): {redact_urls(e)}")

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
