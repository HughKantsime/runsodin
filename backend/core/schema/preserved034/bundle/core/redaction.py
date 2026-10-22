"""Redact credentials from text that may embed destination URLs (logs, stored reasons).

Webhook URLs carry their secret in the path (Discord/Slack tokens), the query
string, or userinfo; Telegram bot tokens appear as bot<id>:<secret>. Only the
scheme and host[:port] survive.
"""
import re
from urllib.parse import urlsplit

_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
_BOT_TOKEN = re.compile(r"\bbot\d+:[A-Za-z0-9_-]+")


def _redact_url(match) -> str:
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        if not host:
            return "[redacted-url]"
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "[redacted-url]"
    rest = "/[redacted]" if (parts.path not in ("", "/") or parts.query or parts.fragment) else ""
    return f"{parts.scheme}://{host}{port}{rest}"


def redact_urls(value) -> str:
    text = "" if value is None else str(value)
    text = _BOT_TOKEN.sub("bot[redacted]", text)
    return _URL.sub(_redact_url, text)
