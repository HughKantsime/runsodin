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

Redaction never raises into the code that logs: every step falls back to
redacting str() of the value, and finally to the redacted placeholder.
Records stay picklable (QueueHandler, multiprocessing): the fallback is a
module-level LogRecord subclass, not a per-record closure.
"""
import logging
import re

REDACTED = "[REDACTED]"

_SECRET_PARAMS = ("token", "access_token", "api_key", "apikey", "key", "ws_token", "oidc_code", "code", "state",
                  "nonce", "password", "pwd", "passwd", "access_code", "refresh_token", "id_token", "client_secret",
                  "private_key", "sign", "deviceId", "devicecrt", "devicepk")
# Credential-bearing query parameters, in a URL or a logged request line.
_QUERY_SECRET = re.compile(r"(?i)([?&](?:" + "|".join(_SECRET_PARAMS) + r")=)[^&\s\"'#]+")
# The same parameters URL-encoded inside another value (?next=%2Fcb%3Fcode%3D...):
# %3F is '?', %26 is '&', %3D is '='.
_ENCODED_QUERY_SECRET = re.compile(r"(?i)((?:%3F|%26)(?:" + "|".join(_SECRET_PARAMS) + r")%3D)(?:(?!%26|%23)[^&\s\"'#])+")
# Userinfo of any URL: scheme://user:pass@ or scheme://token@.
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,31}+://)[^\s/@\"']{1,512}+@")
# Authorization (any scheme) and X-API-Key header values, as logged in dicts or lines.
_QUOTE = "[\"']{0,1}"
_AUTH_HEADER = re.compile(r"(?i)(\bauthorization\b" + _QUOTE + r"\s*[:=]\s*" + _QUOTE + r")(?:[A-Za-z][A-Za-z0-9._-]*\s+){0,1}[^\s\"',;}]+")
_API_KEY_HEADER = re.compile(r"(?i)(\bx-api-key\b" + _QUOTE + r"\s*[:=]\s*" + _QUOTE + r")[^\s\"',;}]+")
# Scoped API tokens: odin_ + secrets.token_urlsafe(32) (43 url-safe characters).
# Short odin_ identifiers (odin_api, odin_schema_migrations) are not secrets.
_API_TOKEN = re.compile(r"\bodin_[A-Za-z0-9_-]{40,}")
# UniFi Protect RTSP(S) path tokens (rtsps://host:7441/<token>, also :7447), as
# camera URLs carry them (modules/printers/route_utils.py _PATH_TOKEN_RE).
_UNIFI_PATH_TOKEN = re.compile(r"(?i)(\brtsp[sx]?://(?:[^\s/?#\"'@]{1,512}+@)?+[^\s/?#\"':@]{1,255}+:(?:7441|7447)/)[A-Za-z0-9]{8,}+")
# Any rtsp(s) URL whose single path segment is a long opaque token (UniFi Protect
# shares, with or without an explicit port; logs cannot tell which host is a UniFi
# source, so every such URL is treated as one). Ordinary stream paths (stream1,
# live/ch0, h264Preview_01_main) are short or segmented and stay readable.
_RTSP_OPAQUE_PATH = re.compile(r"(?i)(\brtsp[sx]?://(?:[^\s/?#\"'@]{1,512}+@)?+[^\s/?#\"'@]{1,255}+/)[A-Za-z0-9]{16,}+(?=$|[?#\s\"'])")
# The internal camera relay's per-printer HMAC token travels in its path (go2rtc's
# ffmpeg and native HTTP sources cannot send headers): /internal/camera-source/<id>/<token>.
_RELAY_PATH_TOKEN = re.compile(r"(/internal/camera-source/\d{1,20}+/)[^\s/?#\"']{1,256}+")
# key=value / key: value pairs naming a credential, anywhere in a line (bare
# token=..., password: ..., a dict repr's 'token': '...').
_KEY_VALUE_NAMES = ("password", "passwd", "pwd", "secret", "token", "access_token", "refresh_token", "id_token",
                    "client_secret", "private_key", "api_key", "apikey", "access_code", "session", "csrf_token",
                    "ws_token", "oidc_code", "deviceId", "devicecrt", "devicepk")
_KEY_VALUE_SECRET = re.compile(r"(?i)(?<![A-Za-z0-9_-])((?:" + "|".join(_KEY_VALUE_NAMES)
                               + r")[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}]+")
# Cookie / Set-Cookie header values: everything to the end of the value.
_COOKIE_HEADER = re.compile(r"(?i)(\b(?:set-)?cookie[\"']?\s*[:=]\s*[\"']?)[^\r\n\"']+")
# JWTs; possessive quantifiers keep matching linear on hostile input.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]++\.eyJ[A-Za-z0-9_-]++\.[A-Za-z0-9_-]++")
_SECRET_KEY_NAMES = ("authorization", "proxy-authorization", "x-api-key", "cookie", "set-cookie", "password", "token",
                     "api_key", "secret", "access_token", "refresh_token", "id_token", "client_secret", "private_key",
                     "session", "deviceId", "devicecrt", "devicepk")
_SECRET_KEYS = re.compile(r"(?i)^(" + "|".join(re.escape(name) for name in _SECRET_KEY_NAMES) + ")$")


_ANYCUBIC_UPLOAD = re.compile(r"(?i)(\bgcode_upload\?)[^\s\"\'<>#]+")


def redact_text(value: str) -> str:
    if not isinstance(value, str):
        value = _safe_str(value)
    value = re.sub(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", REDACTED, value, flags=re.DOTALL)
    value = _ANYCUBIC_UPLOAD.sub(lambda m: m.group(1) + REDACTED, value)
    value = _RELAY_PATH_TOKEN.sub(lambda m: m.group(1) + REDACTED, value)
    value = _UNIFI_PATH_TOKEN.sub(lambda m: m.group(1) + REDACTED, value)
    value = _RTSP_OPAQUE_PATH.sub(lambda m: m.group(1) + REDACTED, value)
    value = _COOKIE_HEADER.sub(lambda m: m.group(1) + REDACTED, value)
    value = _KEY_VALUE_SECRET.sub(lambda m: m.group(1) + REDACTED, value)
    value = _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, value)
    value = _ENCODED_QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, value)
    value = _USERINFO.sub(lambda m: m.group(1) + REDACTED + "@", value)
    value = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, value)
    value = _API_KEY_HEADER.sub(lambda m: m.group(1) + REDACTED, value)
    value = _API_TOKEN.sub(REDACTED, value)
    return _JWT.sub(REDACTED, value)


def _safe_str(value) -> str:
    try:
        return str(value)
    except Exception:
        return REDACTED


_MAX_DEPTH = 32


def _rebuild(value, depth, active):
    if isinstance(value, str):
        return redact_text(value)
    if value is None or type(value) in (bool, int, float):
        return value
    if not isinstance(value, (dict, list, tuple)):
        # Any other object is stringified when logged or serialized: redact that text.
        return redact_text(_safe_str(value))
    if depth > _MAX_DEPTH or id(value) in active:
        return REDACTED  # cycle or absurd nesting: never recurse without bound
    active.add(id(value))
    try:
        if isinstance(value, dict):
            items = {k: (REDACTED if isinstance(k, str) and _SECRET_KEYS.match(k) and v is not None
                         else _rebuild(v, depth + 1, active)) for k, v in value.items()}
            if type(value) is dict:
                return items
            try:
                return type(value)(items)
            except Exception:
                return items
        parts = [_rebuild(v, depth + 1, active) for v in value]
        if type(value) in (list, tuple):
            return type(value)(parts)
        if hasattr(value, "_make"):  # namedtuple
            return value._make(parts)
        try:
            return type(value)(parts)
        except Exception:
            return list(parts) if isinstance(value, list) else tuple(parts)
    finally:
        active.discard(id(value))


def redact_structure(value):
    """Recursively redact strings inside dicts/lists/tuples (audit details, bundle
    JSON, structured log messages); values under credential keys are replaced.
    Never raises: on failure the value's str() is redacted instead."""
    try:
        return _rebuild(value, 0, set())
    except Exception:
        return redact_text(_safe_str(value))


class RedactingLogRecord(logging.LogRecord):
    """getMessage() returns redacted text. Module-level, so records pickle."""

    _odin_redacted = True

    def getMessage(self):
        message = super().getMessage()
        try:
            return redact_text(message)
        except Exception:
            return REDACTED


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Creation-time fallback: structured messages redacted in place; getMessage()
    returns redacted text. Arguments are left intact for formatting. Never raises."""
    try:
        if getattr(record, "_odin_redacted", False):
            return record
        if isinstance(record.msg, (dict, list, tuple)):
            record.msg = redact_structure(record.msg)
        if isinstance(record.args, dict):
            record.args = redact_structure(record.args)
        if type(record) is logging.LogRecord:
            record.__class__ = RedactingLogRecord
        # A custom record class keeps its own getMessage; Handler.format still
        # redacts everything it formats.
    except Exception:
        pass
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
        formatted = original_format(self, record)
        try:
            return redact_text(formatted)
        except Exception:
            return REDACTED
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


class _RedactingFactory:
    """Wraps the previous record factory (module-level class, no closure)."""

    _odin_redacting = True

    def __init__(self, inner):
        self.inner = inner

    def __call__(self, *args, **kwargs):
        return redact_record(self.inner(*args, **kwargs))


def install_log_redaction() -> None:
    """Idempotent; call at every process entry point (API, daemons, monitors)."""
    global _installed
    if not getattr(logging.Handler.format, "_odin_redacting", False):
        _patch_handlers()
    current = logging.getLogRecordFactory()
    if not getattr(current, "_odin_redacting", False):
        logging.setLogRecordFactory(_RedactingFactory(current))
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
    _installed = True
