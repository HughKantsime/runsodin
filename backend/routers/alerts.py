"""O.D.I.N. — Alerts, Webhooks, Push Notifications & SMTP Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
from datetime import datetime
import json
import logging
import threading

from deps import get_db, get_current_user, require_role, log_audit, _get_org_filter
from models import (
    Alert, AlertType, AlertSeverity, AlertPreference, PushSubscription,
    SystemConfig,
)
from schemas import (
    AlertResponse, AlertSummary,
    AlertPreferenceResponse, AlertPreferencesUpdate,
    SmtpConfigBase, SmtpConfigResponse,
    PushSubscriptionCreate,
)
from config import settings

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Webhooks ==============

@router.get("/webhooks", tags=["Webhooks"])
async def list_webhooks(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """List all webhooks."""
    rows = db.execute(text("SELECT * FROM webhooks ORDER BY name")).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/webhooks", tags=["Webhooks"])
async def create_webhook(
    request: Request,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Create a new webhook."""
    data = await request.json()

    name = data.get("name", "Webhook")
    url = data.get("url")
    webhook_type = data.get("webhook_type", "discord")
    alert_types = data.get("alert_types")  # JSON array or comma-separated

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    # Store alert_types as JSON
    if isinstance(alert_types, list):
        alert_types = json.dumps(alert_types)

    db.execute(text("""
        INSERT INTO webhooks (name, url, webhook_type, alert_types)
        VALUES (:name, :url, :type, :alerts)
    """), {"name": name, "url": url, "type": webhook_type, "alerts": alert_types})
    db.commit()

    return {"success": True, "message": "Webhook created"}


@router.patch("/webhooks/{webhook_id}", tags=["Webhooks"])
async def update_webhook(
    webhook_id: int,
    request: Request,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Update a webhook."""
    data = await request.json()

    updates = []
    params = {"id": webhook_id}

    for field in ["name", "url", "webhook_type", "is_enabled", "alert_types"]:
        if field in data:
            value = data[field]
            if field == "alert_types" and isinstance(value, list):
                value = json.dumps(value)
            updates.append(f"{field} = :{field}")
            params[field] = value

    if updates:
        updates.append("updated_at = datetime('now')")
        query = f"UPDATE webhooks SET {', '.join(updates)} WHERE id = :id"
        db.execute(text(query), params)
        db.commit()

    return {"success": True}


@router.delete("/webhooks/{webhook_id}", tags=["Webhooks"])
async def delete_webhook(
    webhook_id: int,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Delete a webhook."""
    db.execute(text("DELETE FROM webhooks WHERE id = :id"), {"id": webhook_id})
    db.commit()
    return {"success": True}


@router.post("/webhooks/{webhook_id}/test", tags=["Webhooks"])
async def test_webhook(
    webhook_id: int,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Send a test message to webhook."""
    row = db.execute(text("SELECT * FROM webhooks WHERE id = :id"), {"id": webhook_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")

    webhook = dict(row._mapping)

    try:
        import httpx

        wtype = webhook["webhook_type"]

        if wtype == "discord":
            payload = {
                "embeds": [{
                    "title": "\U0001f5a8\ufe0f O.D.I.N. Test",
                    "description": "Webhook connection successful!",
                    "color": 0xd97706,
                    "footer": {"text": "O.D.I.N."}
                }]
            }
            resp = httpx.post(webhook["url"], json=payload, timeout=10)

        elif wtype == "slack":
            payload = {
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "\U0001f5a8\ufe0f O.D.I.N. Test"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "Webhook connection successful!"}}
                ]
            }
            resp = httpx.post(webhook["url"], json=payload, timeout=10)

        elif wtype == "ntfy":
            # ntfy: URL is the topic endpoint (e.g., https://ntfy.sh/my-printfarm)
            resp = httpx.post(
                webhook["url"],
                content="Webhook connection successful!",
                headers={
                    "Title": "O.D.I.N. Test",
                    "Priority": "default",
                    "Tags": "white_check_mark,printer",
                },
                timeout=10
            )

        elif wtype == "telegram":
            # Telegram: URL format is https://api.telegram.org/bot<TOKEN>/sendMessage
            # User stores just the bot token + chat_id in the URL as:
            #   bot_token|chat_id  (we parse and construct the API call)
            # OR they can store the full URL with chat_id as a query param
            url = webhook["url"]
            if "|" in url:
                # Format: bot_token|chat_id
                bot_token, chat_id = url.split("|", 1)
                api_url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
            else:
                # Assume full URL, extract chat_id from stored data
                # Fallback: treat URL as bot token, chat_id from name field
                api_url = f"https://api.telegram.org/bot{url.strip()}/sendMessage"
                chat_id = webhook.get("name", "").split("|")[-1] if "|" in webhook.get("name", "") else ""

            resp = httpx.post(
                api_url,
                json={
                    "chat_id": chat_id.strip(),
                    "text": "\U0001f5a8\ufe0f *O.D.I.N. Test*\nWebhook connection successful!",
                    "parse_mode": "Markdown"
                },
                timeout=10
            )

        else:
            # Generic webhook — POST JSON
            payload = {
                "event": "test",
                "source": "odin",
                "message": "Webhook connection successful!"
            }
            resp = httpx.post(webhook["url"], json=payload, timeout=10)

        if resp.status_code in (200, 204):
            return {"success": True, "message": "Test message sent"}
        else:
            return {"success": False, "message": f"Failed: HTTP {resp.status_code} - {resp.text[:200]}"}

    except Exception as e:
        return {"success": False, "message": str(e)}


# ============== Webhook Alert Dispatch ==============

def _dispatch_to_webhooks(db, alert_type_value: str, title: str, message: str, severity: str):
    """Send alert to all matching enabled webhooks."""
    import httpx

    rows = db.execute(text("SELECT * FROM webhooks WHERE is_enabled = 1")).fetchall()

    for row in rows:
        wh = dict(row._mapping)

        # Check if this webhook subscribes to this alert type
        alert_types = wh.get("alert_types")
        if alert_types:
            try:
                types_list = json.loads(alert_types) if isinstance(alert_types, str) else alert_types
                if alert_type_value not in types_list and "all" not in types_list:
                    continue
            except (json.JSONDecodeError, TypeError):
                pass

        wtype = wh["webhook_type"]
        url = wh["url"]

        severity_colors = {"critical": 0xef4444, "warning": 0xf59e0b, "info": 0x3b82f6}
        severity_emoji = {"critical": "\U0001f534", "warning": "\U0001f7e1", "info": "\U0001f535"}
        emoji = severity_emoji.get(severity, "\U0001f535")
        color = severity_colors.get(severity, 0x3b82f6)

        def _send(wtype=wtype, url=url):
            try:
                if wtype == "discord":
                    httpx.post(url, json={
                        "embeds": [{
                            "title": f"{emoji} {title}",
                            "description": message or "",
                            "color": color,
                            "footer": {"text": "O.D.I.N."}
                        }]
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
                        "Title": title,
                        "Priority": priority_map.get(severity, "default"),
                        "Tags": "printer",
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
                            "chat_id": chat_id.strip(),
                            "text": f"{emoji} *{title}*\n{message or ''}",
                            "parse_mode": "Markdown"
                        }, timeout=10)

                else:
                    httpx.post(url, json={
                        "event": alert_type_value,
                        "title": title,
                        "message": message or "",
                        "severity": severity
                    }, timeout=10)

            except Exception as e:
                log.error(f"Webhook dispatch failed ({wtype}): {e}")

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()


# ============== Alerts ==============

@router.get("/alerts", response_model=List[AlertResponse], tags=["Alerts"])
async def list_alerts(
    severity: Optional[str] = None,
    alert_type: Optional[str] = None,
    is_read: Optional[bool] = None,
    limit: int = 25,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List alerts for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    query = db.query(Alert).filter(Alert.user_id == current_user["id"])

    if severity:
        query = query.filter(Alert.severity == severity)
    if alert_type:
        query = query.filter(Alert.alert_type == alert_type)
    if is_read is not None:
        query = query.filter(Alert.is_read == is_read)

    alerts = query.order_by(Alert.created_at.desc()).offset(offset).limit(limit).all()

    results = []
    for alert in alerts:
        data = AlertResponse.model_validate(alert)
        if alert.printer:
            data.printer_name = alert.printer.nickname or alert.printer.name
        if alert.job:
            data.job_name = alert.job.item_name
        if alert.spool and alert.spool.filament:
            data.spool_name = f"{alert.spool.filament.brand} {alert.spool.filament.name}"
        results.append(data)

    return results


@router.get("/alerts/unread-count", tags=["Alerts"])
async def get_unread_count(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get unread alert count for bell badge."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    count = db.query(Alert).filter(
        Alert.user_id == current_user["id"],
        Alert.is_read == False
    ).count()
    return {"unread_count": count}


@router.get("/alerts/summary", response_model=AlertSummary, tags=["Alerts"])
async def get_alert_summary(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get aggregated alert counts for dashboard widget."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    uid = current_user["id"]
    base = db.query(Alert).filter(
        Alert.user_id == uid,
        Alert.is_dismissed == False,
        Alert.is_read == False
    )

    failed = base.filter(Alert.alert_type == AlertType.PRINT_FAILED).count()
    spool = base.filter(Alert.alert_type == AlertType.SPOOL_LOW).count()
    maint = base.filter(Alert.alert_type == AlertType.MAINTENANCE_OVERDUE).count()

    return AlertSummary(
        print_failed=failed,
        spool_low=spool,
        maintenance_overdue=maint,
        total=failed + spool + maint
    )


@router.patch("/alerts/{alert_id}/read", tags=["Alerts"])
async def mark_alert_read(
    alert_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark a single alert as read."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    alert = db.query(Alert).filter(
        Alert.id == alert_id,
        Alert.user_id == current_user["id"]
    ).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_read = True
    db.commit()
    return {"status": "ok"}


@router.post("/alerts/mark-all-read", tags=["Alerts"])
async def mark_all_read(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark all alerts as read for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.query(Alert).filter(
        Alert.user_id == current_user["id"],
        Alert.is_read == False
    ).update({"is_read": True})
    db.commit()
    return {"status": "ok"}


@router.patch("/alerts/{alert_id}/dismiss", tags=["Alerts"])
async def dismiss_alert(
    alert_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Dismiss an alert (hide from dashboard widget)."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    alert = db.query(Alert).filter(
        Alert.id == alert_id,
        Alert.user_id == current_user["id"]
    ).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_dismissed = True
    alert.is_read = True
    db.commit()
    return {"status": "ok"}


# ============== Alert Preferences ==============

@router.get("/alert-preferences", response_model=List[AlertPreferenceResponse], tags=["Alerts"])
async def get_alert_preferences(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get current user's alert preferences."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    prefs = db.query(AlertPreference).filter(
        AlertPreference.user_id == current_user["id"]
    ).all()

    if not prefs:
        from alert_dispatcher import seed_alert_preferences
        seed_alert_preferences(db, current_user["id"])
        prefs = db.query(AlertPreference).filter(
            AlertPreference.user_id == current_user["id"]
        ).all()

    return prefs


@router.put("/alert-preferences", tags=["Alerts"])
async def update_alert_preferences(
    data: AlertPreferencesUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Bulk update alert preferences for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    uid = current_user["id"]
    for pref_data in data.preferences:
        existing = db.query(AlertPreference).filter(
            AlertPreference.user_id == uid,
            AlertPreference.alert_type == pref_data.alert_type
        ).first()

        if existing:
            existing.in_app = pref_data.in_app
            existing.browser_push = pref_data.browser_push
            existing.email = pref_data.email
            existing.threshold_value = pref_data.threshold_value
        else:
            db.add(AlertPreference(
                user_id=uid,
                alert_type=pref_data.alert_type,
                in_app=pref_data.in_app,
                browser_push=pref_data.browser_push,
                email=pref_data.email,
                threshold_value=pref_data.threshold_value
            ))

    db.commit()
    return {"status": "ok", "message": "Preferences updated"}


# ============== SMTP Config (Admin Only) ==============

@router.get("/smtp-config", tags=["Alerts"])
async def get_smtp_config(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Get SMTP configuration (admin only, password masked)."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    if not config:
        return SmtpConfigResponse()
    smtp = config.value
    return SmtpConfigResponse(
        enabled=smtp.get("enabled", False),
        host=smtp.get("host", ""),
        port=smtp.get("port", 587),
        username=smtp.get("username", ""),
        password_set=bool(smtp.get("password")),
        from_address=smtp.get("from_address", ""),
        use_tls=smtp.get("use_tls", True)
    )


@router.put("/smtp-config", tags=["Alerts"])
async def update_smtp_config(
    data: SmtpConfigBase,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Update SMTP configuration (admin only)."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    smtp_data = data.dict()

    if not smtp_data.get("password") and config and config.value.get("password"):
        smtp_data["password"] = config.value["password"]

    if config:
        config.value = smtp_data
    else:
        db.add(SystemConfig(key="smtp_config", value=smtp_data))

    db.commit()
    return {"status": "ok", "message": "SMTP configuration updated"}


@router.post("/alerts/test-email", tags=["Alerts"])
async def send_test_email(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Send a test email to the current user (admin only)."""
    from alert_dispatcher import _get_smtp_config, _deliver_email

    smtp = _get_smtp_config(db)
    if not smtp:
        raise HTTPException(status_code=400, detail="SMTP not configured or not enabled")
    if not current_user.get("email"):
        raise HTTPException(status_code=400, detail="Your account has no email address")

    _deliver_email(
        db, current_user["id"],
        "Test Alert \u2014 O.D.I.N.",
        "This is a test notification. If you received this, SMTP is configured correctly.",
        "info"
    )
    return {"status": "ok", "message": f"Test email queued to {current_user['email']}"}


# ============== Browser Push Subscription ==============

@router.get("/push/vapid-key", tags=["Alerts"])
async def get_vapid_key(db: Session = Depends(get_db)):
    """Get VAPID public key for browser push subscription."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'vapid_keys'")).fetchone()
    if not row:
        return {"public_key": None, "enabled": False}
    try:
        keys = json.loads(row[0])
        return {"public_key": keys.get("public_key"), "enabled": True}
    except Exception:
        return {"public_key": None, "enabled": False}


@router.post("/push/subscribe", tags=["Alerts"])
async def subscribe_push(
    data: PushSubscriptionCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Store a browser push subscription for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    existing = db.query(PushSubscription).filter(
        PushSubscription.user_id == current_user["id"],
        PushSubscription.endpoint == data.endpoint
    ).first()

    if existing:
        existing.p256dh_key = data.p256dh_key
        existing.auth_key = data.auth_key
    else:
        db.add(PushSubscription(
            user_id=current_user["id"],
            endpoint=data.endpoint,
            p256dh_key=data.p256dh_key,
            auth_key=data.auth_key
        ))

    db.commit()
    return {"status": "ok", "message": "Push subscription registered"}


@router.delete("/push/subscribe", tags=["Alerts"])
async def unsubscribe_push(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remove all push subscriptions for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.query(PushSubscription).filter(
        PushSubscription.user_id == current_user["id"]
    ).delete()
    db.commit()
    return {"status": "ok", "message": "Push subscriptions removed"}
