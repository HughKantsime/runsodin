"""Credential redaction for everything ODIN writes to logs, audit rows and bundles.

Browser carriers that cannot send headers put credentials in the URL: the OBS
overlay camera (?token=odin_...), timelapse <video> sources, the WebSocket
(?token=<jwt>) and OIDC callbacks (?code=&state=). Those URLs, and logged
request headers, reach logs verbatim unless redacted when the record is made.
"""
import logging
import re

REDACTED = "[REDACTED]"

_SECRET_PARAMS = ("token", "access_token", "api_key", "apikey", "key", "ws_token", "oidc_code", "code", "state",
                  "nonce", "password", "refresh_token", "id_token", "client_secret")
# Credential-bearing query parameters, in a URL or a logged request line.
_QUERY_SECRET = re.compile(r"(?i)([?&](?:" + "|".join(_SECRET_PARAMS) + r")=)[^&\s\"'#]+")
# Authorization (any scheme) and X-API-Key header values, as logged in dicts or lines.
_QUOTE = "[\"']{0,1}"
_AUTH_HEADER = re.compile(r"(?i)(\bauthorization\b" + _QUOTE + r"\s*[:=]\s*" + _QUOTE + r")(?:[A-Za-z][A-Za-z0-9._-]*\s+){0,1}[^\s\"',;}]+")
_API_KEY_HEADER = re.compile(r"(?i)(\bx-api-key\b" + _QUOTE + r"\s*[:=]\s*" + _QUOTE + r")[^\s\"',;}]+")
# Scoped API tokens: odin_ + secrets.token_urlsafe(32) (43 url-safe characters).
# Short odin_ identifiers (odin_api, odin_schema_migrations) are not secrets.
_API_TOKEN = re.compile(r"\bodin_[A-Za-z0-9_-]{40,}")
# JWTs; possessive quantifiers keep matching linear on hostile input.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]++\.eyJ[A-Za-z0-9_-]++\.[A-Za-z0-9_-]++")


def redact_text(value: str) -> str:
    value = _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, value)
    value = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, value)
    value = _API_KEY_HEADER.sub(lambda m: m.group(1) + REDACTED, value)
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


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Redact message, arguments and traceback text of one record, in place."""
    if getattr(record, "_odin_redacted", False):
        return record
    record.msg = redact_text(record.msg if isinstance(record.msg, str) else str(record.msg))
    if isinstance(record.args, tuple):
        record.args = tuple(redact_text(a) if isinstance(a, str) else a for a in record.args)
    elif isinstance(record.args, dict):
        record.args = {k: redact_text(v) if isinstance(v, str) else v for k, v in record.args.items()}
    if record.exc_info and not record.exc_text:
        # Formatters reuse exc_text; render the traceback once, redacted.
        record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
    elif record.exc_text:
        record.exc_text = redact_text(record.exc_text)
    if record.stack_info:
        record.stack_info = redact_text(record.stack_info)
    record._odin_redacted = True
    return record


class SecretRedactionFilter(logging.Filter):
    """Belt and braces for records not built by the record factory below."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


_FILTER = SecretRedactionFilter()
_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "")


def install_log_redaction() -> None:
    """Idempotent. Every record from any logger is redacted as it is created
    (record factory), so handlers added later, module basicConfig calls and
    third-party loggers are covered; uvicorn's loggers also get the filter."""
    current = logging.getLogRecordFactory()
    if not getattr(current, "_odin_redacting", False):
        def factory(*args, **kwargs):
            return redact_record(current(*args, **kwargs))
        factory._odin_redacting = True
        logging.setLogRecordFactory(factory)
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
        for handler in logger.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
