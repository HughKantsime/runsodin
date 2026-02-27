"""
Notification delivery channels.

Provides send_push_notification(), send_webhook(), send_email().
Called by alert_dispatch and job_events when alerts are created.
"""

import json
import logging
import smtplib
import threading
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import core.crypto as crypto
from core.db_utils import get_db

log = logging.getLogger("printer_events")


def send_push_notification(user_id: int, alert_type: str, title: str, message: str,
                           alert_id: int = None, printer_id: int = None, job_id: int = None):
    """Send push notification to user's subscribed devices."""

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


def _decrypt_webhook_url(url: str) -> str:
    """Decrypt a Fernet-encrypted webhook URL, falling back to plaintext."""
    if not url:
        return url
    try:
        return crypto.decrypt(url)
    except Exception:
        return url


def send_webhook(alert_type: str, title: str, message: str, severity: str = "info",
                 printer_id: int = None, job_id: int = None):
    """Send alert to all matching enabled webhooks.

    Supports discord, slack, ntfy, telegram, pushover, whatsapp, and generic.
    Each webhook fires in a background thread to avoid blocking the caller.
    Daemon-safe: uses raw SQL via get_db().
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, url, webhook_type, alert_types FROM webhooks WHERE is_enabled = 1")
            webhooks = cur.fetchall()
    except Exception as e:
        log.error(f"Failed to read webhooks: {e}")
        return

    severity_colors = {"critical": 0xef4444, "error": 0xe74c3c, "warning": 0xf59e0b, "info": 0x3b82f6}
    severity_emoji = {"critical": "\U0001f534", "error": "\U0001f534", "warning": "\U0001f7e1", "info": "\U0001f535"}

    for webhook_id, raw_url, wtype, alert_types_json in webhooks:
        # Filter by alert_types
        if alert_types_json:
            try:
                allowed = json.loads(alert_types_json) if isinstance(alert_types_json, str) else alert_types_json
                if alert_type not in allowed and "all" not in allowed:
                    continue
            except (json.JSONDecodeError, TypeError):
                pass

        url = _decrypt_webhook_url(raw_url)
        emoji = severity_emoji.get(severity, "\U0001f535")
        color = severity_colors.get(severity, 0x3b82f6)

        def _send(wtype=wtype, url=url, emoji=emoji, color=color):
            try:
                import httpx

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
                    priority_map = {"critical": "urgent", "error": "high", "warning": "high", "info": "default"}
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

                else:  # generic
                    httpx.post(url, json={
                        "event": alert_type,
                        "title": title,
                        "message": message or "",
                        "severity": severity
                    }, timeout=10)

                log.debug(f"Webhook sent to {wtype}")

            except Exception as e:
                log.error(f"Webhook dispatch failed ({wtype}): {e}")

        threading.Thread(target=_send, daemon=True).start()


def send_email(user_id: int, alert_type: str, title: str, message: str,
               printer_id: int = None, job_id: int = None):
    """Send email notification to user."""

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
