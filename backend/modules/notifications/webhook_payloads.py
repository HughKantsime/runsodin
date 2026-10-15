"""Typed, synchronous webhook delivery with a real success signal.

Used where the caller must know whether the receiver accepted the message
(quiet-hours digests record delivery status). Each webhook type gets the
payload shape its receiver requires; success means an HTTP 2xx answer.

User-supplied URLs (generic, Discord, Slack, ntfy) go through safe_post
(SSRF/DNS-pin defense); the fixed provider APIs (Telegram, Pushover,
WhatsApp) go through trusted_post, exactly as the real-time channels do.
"""

import logging
from typing import Optional

from core.redaction import redact_urls
from core.webhook_utils import WebhookSSRFError, safe_post, trusted_post

log = logging.getLogger("odin.notifications")

_SEVERITY_COLORS = {"critical": 0xEF4444, "error": 0xE74C3C, "warning": 0xF59E0B, "info": 0x3B82F6}
_SEVERITY_EMOJI = {"critical": "\U0001f534", "error": "\U0001f534", "warning": "\U0001f7e1", "info": "\U0001f535"}
_NTFY_PRIORITY = {"critical": "urgent", "error": "high", "warning": "high", "info": "default"}


def _accepted(response) -> bool:
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and 200 <= status < 300


def post_typed_webhook(url: str, wtype: Optional[str], *, event: str, title: str, message: str,
                       severity: str = "info", generic_body: Optional[dict] = None) -> bool:
    """POST one message to a webhook in its type's payload shape.

    Returns True only when the receiver answered 2xx. Never raises: SSRF
    refusals, transport errors and malformed provider credentials return False.
    ``generic_body`` replaces the default generic JSON body when given.
    """
    wtype = (wtype or "generic").lower()
    emoji = _SEVERITY_EMOJI.get(severity, "\U0001f535")
    color = _SEVERITY_COLORS.get(severity, 0x3B82F6)
    try:
        if wtype == "discord":
            response = safe_post(url, json={"embeds": [{
                "title": f"{emoji} {title}"[:256], "description": (message or "")[:4096],
                "color": color, "footer": {"text": "O.D.I.N."}}]}, timeout=10)
        elif wtype == "slack":
            response = safe_post(url, json={"text": title, "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {title}"[:150]}},
                {"type": "section", "text": {"type": "mrkdwn", "text": (message or title)[:3000]}},
            ]}, timeout=10)
        elif wtype == "ntfy":
            response = safe_post(url, content=message or title, headers={
                "Title": title, "Priority": _NTFY_PRIORITY.get(severity, "default"), "Tags": "printer",
            }, timeout=10)
        elif wtype == "telegram":
            token, sep, chat_id = url.partition("|")
            if not sep or not token.strip() or not chat_id.strip():
                log.error("Telegram webhook credentials are incomplete (expected bot_token|chat_id)")
                return False
            response = trusted_post(f"https://api.telegram.org/bot{token.strip()}/sendMessage", json={
                "chat_id": chat_id.strip(), "text": f"{emoji} {title}\n{message or ''}",
            }, timeout=10)
        elif wtype == "pushover":
            user_key, sep, api_token = url.partition("|")
            if not sep or not user_key.strip() or not api_token.strip():
                log.error("Pushover webhook credentials are incomplete (expected user_key|api_token)")
                return False
            response = trusted_post("https://api.pushover.net/1/messages.json", data={
                "token": api_token.strip(), "user": user_key.strip(), "title": title,
                "message": message or title, "priority": 1 if severity == "critical" else 0,
            }, timeout=10)
        elif wtype == "whatsapp":
            parts = [part.strip() for part in url.split("|")]
            if len(parts) < 3 or not all(parts[:3]):
                log.error("WhatsApp webhook credentials are incomplete (expected phone_id|recipient|token)")
                return False
            phone_id, recipient, token = parts[:3]
            response = trusted_post(
                f"https://graph.facebook.com/v18.0/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"messaging_product": "whatsapp", "to": recipient, "type": "text",
                      "text": {"body": f"{emoji} {title}\n{message or ''}"}},
                timeout=10,
            )
        else:
            body = generic_body if generic_body is not None else {
                "event": event, "title": title, "message": message or "", "severity": severity}
            response = safe_post(url, json=body, timeout=10)
    except WebhookSSRFError as exc:
        log.error(f"Webhook SSRF-blocked ({wtype}): {redact_urls(exc)}")
        return False
    except Exception as exc:
        log.error(f"Webhook delivery failed ({wtype}): {type(exc).__name__}")
        return False
    if not _accepted(response):
        log.error(f"Webhook rejected ({wtype}): HTTP {getattr(response, 'status_code', '?')}")
        return False
    return True
