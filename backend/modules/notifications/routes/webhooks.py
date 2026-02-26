"""O.D.I.N. — Webhook CRUD, Test-Fire, and Alert Dispatch."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
import json
import logging
import threading

import core.crypto as crypto
from core.db import get_db
from core.rbac import require_role
from core.webhook_utils import _validate_webhook_url

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


# ============== Webhooks ==============

@router.get("")
async def list_webhooks(
    current_user: dict = Depends(require_role("admin")),
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


@router.post("")
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

    _validate_webhook_url(url)
    url = _encrypt_webhook_url(url)

    # Store alert_types as JSON
    if isinstance(alert_types, list):
        alert_types = json.dumps(alert_types)

    db.execute(text("""
        INSERT INTO webhooks (name, url, webhook_type, alert_types)
        VALUES (:name, :url, :type, :alerts)
    """), {"name": name, "url": url, "type": webhook_type, "alerts": alert_types})
    db.commit()

    return {"success": True, "message": "Webhook created"}


@router.patch("/{webhook_id}")
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

    if "url" in data and data["url"]:
        _validate_webhook_url(data["url"])

    for field in ["name", "url", "webhook_type", "is_enabled", "alert_types"]:
        if field in data:
            value = data[field]
            if field == "alert_types" and isinstance(value, list):
                value = json.dumps(value)
            if field == "url" and value:
                value = _encrypt_webhook_url(value)
            updates.append(f"{field} = :{field}")
            params[field] = value

    if updates:
        updates.append("updated_at = datetime('now')")
        query = f"UPDATE webhooks SET {', '.join(updates)} WHERE id = :id"
        db.execute(text(query), params)
        db.commit()

    return {"success": True}


@router.delete("/{webhook_id}")
async def delete_webhook(
    webhook_id: int,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Delete a webhook."""
    db.execute(text("DELETE FROM webhooks WHERE id = :id"), {"id": webhook_id})
    db.commit()
    return {"success": True}


@router.post("/{webhook_id}/test")
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
    webhook["url"] = _decrypt_webhook_url(webhook.get("url", ""))

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

        elif wtype == "pushover":
            # Pushover: url format "user_key|api_token"
            if "|" not in webhook["url"]:
                return {"success": False, "message": "Invalid Pushover config. Format: user_key|api_token"}
            user_key, api_token = webhook["url"].split("|", 1)
            resp = httpx.post("https://api.pushover.net/1/messages.json", data={
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
            resp = httpx.post(
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
        url = _decrypt_webhook_url(wh.get("url", ""))

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

                elif wtype == "pushover":
                    # url format: "user_key|api_token"
                    if "|" in url:
                        user_key, api_token = url.split("|", 1)
                        httpx.post("https://api.pushover.net/1/messages.json", data={
                            "token": api_token.strip(),
                            "user": user_key.strip(),
                            "title": title,
                            "message": message or title,
                            "priority": 1 if severity == "critical" else 0,
                        }, timeout=10)

                elif wtype == "whatsapp":
                    # url format: "phone_number_id|recipient_phone|bearer_token"
                    parts = url.split("|")
                    if len(parts) >= 3:
                        phone_id, recipient, token = parts[0].strip(), parts[1].strip(), parts[2].strip()
                        httpx.post(
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
