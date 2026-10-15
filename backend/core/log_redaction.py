"""Credential redaction for everything ODIN writes to logs, audit rows and bundles.

Browser carriers that cannot send headers put credentials in the URL: the OBS
overlay camera (?token=odin_...), timelapse <video> sources and the WebSocket
(?token=<jwt>). Those URLs reach uvicorn's access and error logs verbatim unless
redacted at write time.
"""
import logging
import re

REDACTED = "[REDACTED]"

# Credential-bearing query parameters, in a URL or a logged request line.
_QUERY_SECRET = re.compile(r"(?i)([?&](?:token|access_token|api_key|apikey|key|ws_token)=)[^&\s\"'#]+")
# Scoped API tokens (odin_ + token_urlsafe, which includes '-' and '_') and JWTs anywhere.
_API_TOKEN = re.compile(r"\bodin_[A-Za-z0-9_-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def redact_text(value: str) -> str:
    value = _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, value)
    value = _API_TOKEN.sub(REDACTED, value)
    return _JWT.sub(REDACTED, value)


def redact_structure(value):
    """Recursively redact strings inside dicts/lists (audit details, bundle JSON)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_structure(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_structure(v) for v in value)
    return value


class SecretRedactionFilter(logging.Filter):
    """Rewrites a record's message and string arguments before any handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_text(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_text(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


_FILTER = SecretRedactionFilter()
# uvicorn's access/error loggers do not propagate to root; filter them directly.
_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "")


def install_log_redaction() -> None:
    """Idempotent: attach the filter to uvicorn's loggers and to every root handler."""
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
        for handler in logger.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
