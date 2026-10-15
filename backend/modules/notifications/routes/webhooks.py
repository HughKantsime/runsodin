"""O.D.I.N. — Webhook CRUD, Test-Fire, and Alert Dispatch."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
import json
import logging
import threading

from modules.notifications.event_selection import selects_event, serialize_event_selection

import core.crypto as crypto
from core.db import get_db
from core.db_compat import execute_insert_returning_id, sql
from core.dependencies import log_audit
from core.rbac import require_superadmin, require_scope
from core.license_manager import require_feature, get_license
from core.redaction import redact_urls
from core.webhook_utils import _validate_webhook_url, resolve_and_check_webhook_url, safe_post, trusted_post, WebhookSSRFError

log = logging.getLogger("odin.api")

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def _decrypt_webhook_url(url: str) -> str:
    """Decrypt a webhook URL, returning the plaintext. Migration-safe: returns
    the value unchanged if it is not Fernet-encrypted (pre-v1.3.66 rows)."""
    if not url:
        return url
    try:
        return crypto.decrypt(url)
    except Exception:
        return url  # plaintext fallback for existing rows


def _encrypt_webhook_url(url: str) -> str:
    """Encrypt a webhook URL if not already encrypted."""
    if not url:
        return url
    if crypto.is_encrypted(url):
        return url
    return crypto.encrypt(url)


import re as _re

# Fixed-provider channels store credentials, not URLs (the API host is
# hardcoded in the dispatcher), in the documented pipe-separated formats.
_CREDENTIAL_FORMATS = {
    "telegram": (_re.compile(r"^[A-Za-z0-9:_-]+\|(-?\d+|@[A-Za-z0-9_]{4,})$"),
                 "Telegram credentials must be bot_token|chat_id"),
    "whatsapp": (_re.compile(r"^\d+\|\+?\d{6,15}\|[^|\s]+$"),
                 "WhatsApp credentials must be phone_number_id|recipient_phone|access_token"),
    "pushover": (_re.compile(r"^[A-Za-z0-9_-]+\|[A-Za-z0-9_-]+$"),
                 "Pushover credentials must be user_key|api_token"),
}


def _validate_webhook_destination(url: str, webhook_type: str) -> None:
    """URL types get SSRF validation; fixed-provider types get credential-format validation."""
    fmt = _CREDENTIAL_FORMATS.get((webhook_type or "").lower())
    if fmt is None:
        _validate_webhook_url(url)
        return
    pattern, message = fmt
    if not isinstance(url, str) or not pattern.fullmatch(url.strip()):
        raise HTTPException(status_code=400, detail=message)


# ============== Webhooks ==============

@router.get("")
async def list_webhooks(
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db)
):
    """List all webhooks."""
    rows = db.execute(text("SELECT * FROM webhooks ORDER BY name")).fetchall()
    results = []
    for r in rows:
        wh = dict(r._mapping)
        wh["url"] = _decrypt_webhook_url(wh.get("url", ""))
        results.append(wh)
    return results


@router.post("", dependencies=[Depends(require_scope("write"))])
async def create_webhook(
    request: Request,
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db)
):
    """Create a new webhook."""
    require_feature("webhooks")
    data = await request.json()

    name = data.get("name", "Webhook")
    url = data.get("url")
    webhook_type = data.get("webhook_type", "discord")
    try:
        alert_types = serialize_event_selection(data.get("alert_types"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    _validate_webhook_destination(url, webhook_type)
    url = _encrypt_webhook_url(url.strip() if webhook_type in _CREDENTIAL_FORMATS else url)

    insert_sql = """
        INSERT INTO webhooks (name, url, webhook_type, alert_types)
        VALUES (:name, :url, :type, :alerts)
    """
    params = {"name": name, "url": url, "type": webhook_type, "alerts": alert_types}
    wh_id = execute_insert_returning_id(db, insert_sql, params)
    db.flush()
    log_audit(db, "webhook.created", "webhook", wh_id, {"name": name, "type": webhook_type})
    db.commit()

    return {"success": True, "message": "Webhook created"}


@router.patch("/{webhook_id}", dependencies=[Depends(require_scope("write"))])
async def update_webhook(
    webhook_id: int,
    request: Request,
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db)
):
    """Update a webhook."""
    require_feature("webhooks")
    data = await request.json()

    if "alert_types" in data:
        try:
            data["alert_types"] = serialize_event_selection(data["alert_types"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    updates = []
    params = {"id": webhook_id}

    if ("url" in data and data["url"]) or "webhook_type" in data:
        current = db.execute(text("SELECT url, webhook_type FROM webhooks WHERE id = :id"),
                             {"id": webhook_id}).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Webhook not found")
        target_type = data.get("webhook_type", current.webhook_type)
        target_url = data["url"] if data.get("url") else _decrypt_webhook_url(current.url)
        _validate_webhook_destination(target_url, target_type)

    for field in ["name", "url", "webhook_type", "is_enabled", "alert_types"]:
        if field in data:
            value = data[field]
            if field == "url" and value:
                value = _encrypt_webhook_url(value)
            updates.append(f"{field} = :{field}")
            params[field] = value

    if updates:
        updates.append(f"updated_at = {sql.now()}")
        query = f"UPDATE webhooks SET {', '.join(updates)} WHERE id = :id"
        db.execute(text(query), params)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        log_audit(db, "webhook.updated", "webhook", webhook_id, {"fields": [f for f in data.keys() if f != "url"]})
        db.commit()

    return {"success": True}


@router.delete("/{webhook_id}", dependencies=[Depends(require_scope("write"))])
async def delete_webhook(
    webhook_id: int,
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db)
):
    """Delete a webhook."""
    db.execute(text("DELETE FROM webhooks WHERE id = :id"), {"id": webhook_id})
    log_audit(db, "webhook.deleted", "webhook", webhook_id)
    db.commit()
    return {"success": True}


@router.post("/{webhook_id}/test", dependencies=[Depends(require_scope("write"))])
async def test_webhook(
    webhook_id: int,
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db)
):
    """Send a test message to webhook."""
    require_feature("webhooks")
    row = db.execute(text("SELECT * FROM webhooks WHERE id = :id"), {"id": webhook_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")

    webhook = dict(row._mapping)
    webhook["url"] = _decrypt_webhook_url(webhook.get("url", ""))

    try:
        import httpx

        wtype = webhook["webhook_type"]

        if wtype == "discord":
            # R8: DNS-resolve and reject private targets at dispatch time.
            # Admin-configured URL may have changed DNS between config and now.
            try:
                target_url = resolve_and_check_webhook_url(webhook["url"])
            except WebhookSSRFError as e:
                return {"success": False, "message": f"Webhook refused: {redact_urls(e)}"}
            payload = {
                "embeds": [{
                    "title": "\U0001f5a8\ufe0f O.D.I.N. Test",
                    "description": "Webhook connection successful!",
                    "color": 0xd97706,
                    "footer": {"text": "O.D.I.N."}
                }]
            }
            resp = safe_post(target_url, json=payload, timeout=10)

        elif wtype == "slack":
            try:
                target_url = resolve_and_check_webhook_url(webhook["url"])
            except WebhookSSRFError as e:
                return {"success": False, "message": f"Webhook refused: {redact_urls(e)}"}
            payload = {
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "\U0001f5a8\ufe0f O.D.I.N. Test"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "Webhook connection successful!"}}
                ]
            }
            resp = safe_post(target_url, json=payload, timeout=10)

        elif wtype == "ntfy":
            # ntfy: URL is the topic endpoint (e.g., https://ntfy.sh/my-printfarm)
            try:
                target_url = resolve_and_check_webhook_url(webhook["url"])
            except WebhookSSRFError as e:
                return {"success": False, "message": f"Webhook refused: {redact_urls(e)}"}
            resp = safe_post(
                target_url,
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

            # Hardcoded Telegram bot API — trusted_post lets HTTP_PROXY
            # work for egress-only deployments. (Codex pass 3, 2026-04-13.)
            resp = trusted_post(
                api_url,
                json={
                    "chat_id": chat_id.strip(),
                    "text": "\U0001f5a8\ufe0f *O.D.I.N. Test*\nWebhook connection successful!",
                    "parse_mode": "Markdown"
                },
                timeout=10
            )

        elif wtype == "pushover":
            # Pushover: url format "user_key|api_token"
            if "|" not in webhook["url"]:
                return {"success": False, "message": "Invalid Pushover config. Format: user_key|api_token"}
            user_key, api_token = webhook["url"].split("|", 1)
            # Hardcoded Pushover API — trusted_post for HTTP_PROXY support.
            resp = trusted_post("https://api.pushover.net/1/messages.json", data={
                "token": api_token.strip(),
                "user": user_key.strip(),
                "title": "O.D.I.N. Test",
                "message": "Webhook connection successful!",
            }, timeout=10)

        elif wtype == "whatsapp":
            # WhatsApp: url format "phone_number_id|recipient_phone|bearer_token"
            parts = webhook["url"].split("|")
            if len(parts) < 3:
                return {"success": False, "message": "Invalid WhatsApp config. Format: phone_id|recipient|token"}
            phone_id, recipient, token = parts[0].strip(), parts[1].strip(), parts[2].strip()
            # Hardcoded Meta Graph API — trusted_post for HTTP_PROXY support.
            resp = trusted_post(
                f"https://graph.facebook.com/v18.0/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "messaging_product": "whatsapp",
                    "to": recipient,
                    "type": "text",
                    "text": {"body": "\U0001f5a8\ufe0f O.D.I.N. Test — Webhook connection successful!"},
                },
                timeout=10,
            )

        else:
            # Generic webhook — POST JSON
            payload = {
                "event": "test",
                "source": "odin",
                "message": "Webhook connection successful!"
            }
            resp = safe_post(webhook["url"], json=payload, timeout=10)

        if resp.status_code in (200, 204):
            return {"success": True, "message": "Test message sent"}
        else:
            return {"success": False, "message": f"Failed: HTTP {resp.status_code} - {resp.text[:200]}"}

    except Exception as e:
        log.warning("Webhook test failed: %s", e)
        return {"success": False, "message": "Webhook test failed. Check URL and network connectivity."}


# ============== Webhook Alert Dispatch ==============

def _dispatch_to_webhooks(db, alert_type_value: str, title: str, message: str, severity: str):
    """Send alert to all matching enabled webhooks."""
    if not get_license().has_feature("webhooks"):
        return
    import httpx

    rows = db.execute(text("SELECT * FROM webhooks WHERE is_enabled IS TRUE")).fetchall()

    for row in rows:
        wh = dict(row._mapping)

        if not selects_event(wh.get("alert_types"), alert_type_value):
            continue

        wtype = wh["webhook_type"]
        url = _decrypt_webhook_url(wh.get("url", ""))

        severity_colors = {"critical": 0xef4444, "warning": 0xf59e0b, "info": 0x3b82f6}
        severity_emoji = {"critical": "\U0001f534", "warning": "\U0001f7e1", "info": "\U0001f535"}
        emoji = severity_emoji.get(severity, "\U0001f535")
        color = severity_colors.get(severity, 0x3b82f6)

        def _send(wtype=wtype, url=url):
            try:
                if wtype == "discord":
                    safe_post(url, json={
                        "embeds": [{
                            "title": f"{emoji} {title}",
                            "description": message or "",
                            "color": color,
                            "footer": {"text": "O.D.I.N."}
                        }]
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
                        "Title": title,
                        "Priority": priority_map.get(severity, "default"),
                        "Tags": "printer",
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
                            "chat_id": chat_id.strip(),
                            "text": f"{emoji} *{title}*\n{message or ''}",
                            "parse_mode": "Markdown"
                        }, timeout=10)

                elif wtype == "pushover":
                    # Hardcoded Pushover API — trusted_post for HTTP_PROXY.
                    if "|" in url:
                        user_key, api_token = url.split("|", 1)
                        trusted_post("https://api.pushover.net/1/messages.json", data={
                            "token": api_token.strip(),
                            "user": user_key.strip(),
                            "title": title,
                            "message": message or title,
                            "priority": 1 if severity == "critical" else 0,
                        }, timeout=10)

                elif wtype == "whatsapp":
                    # Hardcoded Meta Graph API — trusted_post for HTTP_PROXY.
                    parts = url.split("|")
                    if len(parts) >= 3:
                        phone_id, recipient, token = parts[0].strip(), parts[1].strip(), parts[2].strip()
                        trusted_post(
                            f"https://graph.facebook.com/v18.0/{phone_id}/messages",
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                            json={
                                "messaging_product": "whatsapp",
                                "to": recipient,
                                "type": "text",
                                "text": {"body": f"{emoji} {title}\n{message or ''}"},
                            },
                            timeout=10,
                        )

                else:
                    safe_post(url, json={
                        "event": alert_type_value,
                        "title": title,
                        "message": message or "",
                        "severity": severity
                    }, timeout=10)

            except Exception as e:
                log.error(f"Webhook dispatch failed ({wtype}): {e}")

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()
