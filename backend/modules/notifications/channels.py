"""
Notification delivery channels.

Provides send_push_notification(), send_webhook(), send_email().
Called by alert_dispatch and job_events when alerts are created.
"""

import json
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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


def send_webhook(alert_type: str, title: str, message: str, severity: str = "info",
                 printer_id: int = None, job_id: int = None):
    """Send alert to configured webhooks."""

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
