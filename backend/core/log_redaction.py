"""Credential redaction for everything ODIN writes to logs, audit rows and bundles.

Browser carriers that cannot send headers put credentials in the URL: the OBS
overlay camera (?token=odin_...), timelapse <video> sources, the WebSocket
(?token=<jwt>) and OIDC callbacks (?code=&state=); camera URLs carry userinfo.
Those URLs, and logged request headers, reach logs verbatim unless redacted.

Redaction works on formatted output (Handler.format), so %-placeholders and
non-str arguments (httpx.URL, lists, dicts, lazy objects) are redacted after
formatting and never break. Record creation adds a fallback for consumers that
read record.getMessage() directly, and redacts structured (dict) messages in
place without turning them into strings.
"""
import logging
import re

REDACTED = "[REDACTED]"

_SECRET_PARAMS = ("token", "access_token", "api_key", "apikey", "key", "ws_token", "oidc_code", "code", "state",
                  "nonce", "password", "pwd", "passwd", "access_code", "refresh_token", "id_token", "client_secret")
# Credential-bearing query parameters, in a URL or a logged request line.
_QUERY_SECRET = re.compile(r"(?i)([?&](?:" + "|".join(_SECRET_PARAMS) + r")=)[^&\s\"'#]+")
# Userinfo of any URL: scheme://user:pass@ or scheme://token@.
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,31}+://)[^\s/@\"']{1,512}+@")
# Authorization (any scheme) and X-API-Key header values, as logged in dicts or lines.
_QUOTE = "[\"']{0,1}"
_AUTH_HEADER = re.compile(r"(?i)(\bauthorization\b" + _QUOTE + r"\s*[:=]\s*" + _QUOTE + r")(?:[A-Za-z][A-Za-z0-9._-]*\s+){0,1}[^\s\"',;}]+")
_API_KEY_HEADER = re.compile(r"(?i)(\bx-api-key\b" + _QUOTE + r"\s*[:=]\s*" + _QUOTE + r")[^\s\"',;}]+")
# Scoped API tokens: odin_ + secrets.token_urlsafe(32) (43 url-safe characters).
# Short odin_ identifiers (odin_api, odin_schema_migrations) are not secrets.
_API_TOKEN = re.compile(r"\bodin_[A-Za-z0-9_-]{40,}")
# JWTs; possessive quantifiers keep matching linear on hostile input.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]++\.eyJ[A-Za-z0-9_-]++\.[A-Za-z0-9_-]++")
_SECRET_KEYS = re.compile(r"(?i)^(authorization|proxy-authorization|x-api-key|cookie|set-cookie|password|token|api_key|secret)$")


def redact_text(value: str) -> str:
    value = _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, value)
    value = _USERINFO.sub(lambda m: m.group(1) + REDACTED + "@", value)
    value = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, value)
    value = _API_KEY_HEADER.sub(lambda m: m.group(1) + REDACTED, value)
    value = _API_TOKEN.sub(REDACTED, value)
    return _JWT.sub(REDACTED, value)


def redact_structure(value):
    """Recursively redact strings inside dicts/lists (audit details, bundle JSON,
    structured log messages); values under credential keys are replaced."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and _SECRET_KEYS.match(k) and v is not None else redact_structure(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_structure(v) for v in value)
    return value


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Creation-time fallback: structured messages redacted in place; getMessage()
    returns redacted text. Arguments are left intact for formatting."""
    if getattr(record, "_odin_redacted", False):
        return record
    if isinstance(record.msg, (dict, list, tuple)):
        record.msg = redact_structure(record.msg)
    if isinstance(record.args, dict):
        record.args = redact_structure(record.args)
    original = record.getMessage

    def get_message():
        return redact_text(original())
    record.getMessage = get_message
    record._odin_redacted = True
    return record


class SecretRedactionFilter(logging.Filter):
    """Kept on uvicorn's loggers as a second layer (same fallback)."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


_FILTER = SecretRedactionFilter()
_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "")
_installed = False


def _patch_handlers() -> None:
    """Every handler, including ones created later, formats through redaction,
    and a failing emit never prints raw arguments."""
    original_format = logging.Handler.format

    def format(self, record):
        return redact_text(original_format(self, record))
    format._odin_redacting = True
    logging.Handler.format = format

    def handle_error(self, record):
        if logging.raiseExceptions:
            import sys
            import traceback
            try:
                exc = sys.exc_info()
                sys.stderr.write("--- Logging error ---\n")
                sys.stderr.write(redact_text("".join(traceback.format_exception(*exc))))
                sys.stderr.write(redact_text(f"Logged from {record.pathname}:{record.lineno}; message: {record.getMessage()}\n"))
            except Exception:  # never let logging errors escape
                pass
    handle_error._odin_redacting = True
    logging.Handler.handleError = handle_error


def install_log_redaction() -> None:
    """Idempotent; call at every process entry point (API, daemons, monitors)."""
    global _installed
    if not getattr(logging.Handler.format, "_odin_redacting", False):
        _patch_handlers()
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
    _installed = True
