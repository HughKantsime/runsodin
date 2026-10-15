"""Shared utilities for printer route files.

These helpers are used by multiple printer route split files and by
external modules (e.g. system/routes.py imports _check_ssrf_blocklist).
"""

import fcntl
import logging
import os
import re
import threading
import time
from urllib.parse import quote as urlquote

import yaml
from fastapi import HTTPException
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import Optional

import core.crypto as crypto

log = logging.getLogger("odin.api")

GO2RTC_CONFIG = os.environ.get("GO2RTC_CONFIG", "/app/go2rtc/go2rtc.yaml")

# go2rtc restart protection — prevents restart storms when multiple
# cameras trigger sync simultaneously (e.g. control room page load).
# Uses a file lock so it works across processes (monitors + backend).
_go2rtc_thread_lock = threading.Lock()
_GO2RTC_MIN_RESTART_INTERVAL = 10.0  # seconds
_GO2RTC_LOCKFILE = os.environ.get("GO2RTC_LOCKFILE", "/tmp/go2rtc_sync.lock")

# ====================================================================
# Inline Pydantic models shared across route files
# ====================================================================

class TestConnectionRequest(PydanticBaseModel):
    """Request body for testing printer connection."""
    api_type: str
    api_host: str
    serial: Optional[str] = None
    access_code: Optional[str] = None


# ====================================================================
# SSRF / URL Validation
# ====================================================================

def _check_ssrf_blocklist(host: str) -> None:
    """Block SSRF attempts targeting localhost, metadata endpoints, or internal-only hosts."""
    import ipaddress
    blocked_prefixes = ("localhost", "127.", "169.254.", "0.", "::1")
    h = (host or "").strip().lower().split(":")[0]  # strip port
    if any(h.startswith(p) for p in blocked_prefixes):
        raise HTTPException(status_code=400, detail="Invalid printer host")
    try:
        addr = ipaddress.ip_address(h)
        if addr.is_loopback or addr.is_link_local:
            raise HTTPException(status_code=400, detail="Invalid printer host")
    except ValueError:
        pass  # hostname — let OS resolve it


# '&' is a query-string separator (cameras take ?user=&password=); the URL is only ever
# written to go2rtc's YAML as an rtsp(s) source, never to a shell.
_CAMERA_SHELL_METACHAR_RE = re.compile(r'[;|$`\\]')
# Discovered (HTTP/ffmpeg) sources also refuse '&': go2rtc ffmpeg sources parse directives.
_CAMERA_DIRECTIVE_RE = re.compile(r'[;&|$`\\]')


def _validate_camera_url(url: str) -> str:
    """Validate and sanitize a camera URL before writing to go2rtc config.

    - Only rtsp:// or rtsps:// schemes allowed
    - Strips shell metacharacters (; & | $ ` \\)
    - Rejects localhost/loopback targets (SSRF prevention)

    Returns the sanitized URL or raises HTTPException 400.
    """
    if not url:
        return url

    import urllib.parse
    stripped = url.strip()
    lower = stripped.lower()
    if not (lower.startswith("rtsp://") or lower.startswith("rtsps://")):
        raise HTTPException(status_code=400, detail="Camera URL must use rtsp:// or rtsps:// scheme")
    # Store the scheme lower-case so every later check sees one spelling.
    scheme_end = stripped.index("://")
    stripped = stripped[:scheme_end].lower() + stripped[scheme_end:]

    sanitized = _CAMERA_SHELL_METACHAR_RE.sub('', stripped)

    try:
        parsed = urllib.parse.urlparse(sanitized)
        host = parsed.hostname or ""
        _check_ssrf_blocklist(host)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Camera URL points to a blocked host")

    return sanitized


# ====================================================================
# Camera / go2rtc helpers
# ====================================================================

def get_camera_url(printer):
    """Get camera URL for a printer - from DB field or auto-generated from credentials."""
    if printer.camera_url:
        url = printer.camera_url
        try:
            url = crypto.decrypt(url)
        except Exception as e:
            log.debug(f"Failed to decrypt camera URL (using raw): {e}")
        return url
    if printer.api_type == "bambu" and printer.api_key and printer.api_host:
        RTSP_MODELS = {'X1C', 'X1 Carbon', 'X1E', 'X1 Carbon Combo', 'H2D'}
        model = (printer.model or '').strip()
        if model not in RTSP_MODELS:
            return None
        try:
            parts = crypto.decrypt(printer.api_key).split("|")
            if len(parts) == 2:
                return f"rtsps://bblp:{urlquote(parts[1], safe='')}@{printer.api_host}:322/streaming/live/1"
        except Exception as e:
            log.debug(f"Failed to decrypt Bambu API key: {e}")
    return None


def effective_camera_url(printer):
    """Stored camera URL (possibly encrypted), else the one generated from the
    printer's credentials. Contains credentials: responses must mask it."""
    return printer.camera_url or get_camera_url(printer)


# Userinfo of any URL scheme, case-insensitive (RTSP://, rtsps://, http://, ...).
_URL_USERINFO_RE = re.compile(r'^([a-z][a-z0-9+.\-]*://)([^@/?#]*)@', re.IGNORECASE)
CAMERA_CREDENTIAL_MASK = '***'


# Query parameters some cameras take credentials in (?user=..&password=..).
_CREDENTIAL_QUERY_RE = re.compile(r'([?&](?:user|username|usr|login|password|pass|passwd|pwd|token|auth|key|secret|api_key|apikey|access_code|sig)=)([^&#]*)', re.IGNORECASE)
# Vendors that put the credential in the path. UniFi Protect: rtsp://host:7447/<token>,
# rtsps://host:7441/<token>[?enableSrtp]. Other path-token schemes are not recognised
# and are stored as entered (documented limitation: use userinfo or query credentials).
_PATH_TOKEN_RE = re.compile(r'^(rtsps?://[^/?#]+:(?:7441|7447)/)([A-Za-z0-9]{8,})(?=$|[?#])', re.IGNORECASE)
_DEFAULT_PORTS = {'rtsp': 554, 'rtsps': 322, 'rtspx': 322, 'http': 80, 'https': 443}


def camera_url_has_credentials(url) -> bool:
    """Userinfo or a credential query parameter: the URL must be stored encrypted."""
    if not isinstance(url, str):
        return False
    return (bool(_URL_USERINFO_RE.match(url.strip())) or any(m.group(2) for m in _CREDENTIAL_QUERY_RE.finditer(url))
            or bool(_PATH_TOKEN_RE.match(url.strip())))


def sanitize_camera_url(url: str) -> str:
    """Mask URL credentials (userinfo and credential query values) for API responses."""
    if not url:
        return url
    masked = _URL_USERINFO_RE.sub(lambda m: m.group(1) + CAMERA_CREDENTIAL_MASK + '@', url.strip(), count=1)
    masked = _PATH_TOKEN_RE.sub(lambda m: m.group(1) + CAMERA_CREDENTIAL_MASK, masked, count=1)
    return _CREDENTIAL_QUERY_RE.sub(lambda m: m.group(1) + (CAMERA_CREDENTIAL_MASK if m.group(2) else ''), masked)


def masked_camera_url(stored):
    """Response form of a stored camera URL: decrypted, credentials masked.

    Never returns ciphertext; an undecryptable stored value returns None.
    """
    if not stored:
        return stored
    plain = crypto.decrypt(stored) if crypto.is_encrypted(stored) else stored
    if not plain or crypto.is_encrypted(plain):
        return None
    return sanitize_camera_url(plain)


def _url_endpoint(url):
    """(scheme, host, port) with the scheme's default port filled in."""
    import urllib.parse
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        return (scheme, (parsed.hostname or '').lower(), parsed.port or _DEFAULT_PORTS.get(scheme))
    except ValueError:
        return None


def camera_url_is_masked(url) -> bool:
    if not isinstance(url, str):
        return False
    match = _URL_USERINFO_RE.match(url.strip())
    return bool(match and match.group(2) == CAMERA_CREDENTIAL_MASK) or any(
        m.group(2) == CAMERA_CREDENTIAL_MASK for m in _CREDENTIAL_QUERY_RE.finditer(url)) or bool(
        re.match(r'^rtsps?://[^/?#]+:(?:7441|7447)/\*\*\*(?=$|[?#])', url.strip(), re.IGNORECASE))


def resolve_masked_camera_url(incoming, stored):
    """Restore credentials when a client sends back a masked camera URL.

    Returns the plaintext URL to save, or None when the client sent back
    exactly the masked form of the stored URL (nothing changed). Stored
    credentials (userinfo and credential query values) are carried over only to
    the same scheme, host and port (default ports are equivalent; a path or
    other parameter change is allowed); pointing a masked URL at another
    endpoint, or masking a value with no stored counterpart, is a 400 that
    requires the full credentialed URL.
    """
    if not camera_url_is_masked(incoming):
        return incoming
    incoming = incoming.strip()
    plain = crypto.decrypt(stored) if stored and crypto.is_encrypted(stored) else stored
    if not plain or crypto.is_encrypted(plain):
        raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
    plain = plain.strip()
    if incoming == sanitize_camera_url(plain):
        return None
    target = _url_endpoint(incoming)
    if target is None or target != _url_endpoint(plain):
        raise HTTPException(status_code=400,
                            detail="Camera URL endpoint changed; enter the full URL including credentials")
    resolved = incoming
    match = _URL_USERINFO_RE.match(resolved)
    if match and match.group(2) == CAMERA_CREDENTIAL_MASK:
        stored_match = _URL_USERINFO_RE.match(plain)
        if not stored_match:
            raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
        resolved = match.group(1).lower() + stored_match.group(2) + '@' + resolved[match.end():]
    path_masked = re.match(r'^(rtsps?://[^/?#]+:(?:7441|7447)/)\*\*\*(?=$|[?#])', resolved, re.IGNORECASE)
    if path_masked:
        stored_path = _PATH_TOKEN_RE.match(plain)
        if not stored_path:
            raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
        resolved = path_masked.group(1) + stored_path.group(2) + resolved[path_masked.end():]
    stored_values = {m.group(1)[1:].lower(): m.group(2) for m in _CREDENTIAL_QUERY_RE.finditer(plain)}

    def restore(m):
        if m.group(2) != CAMERA_CREDENTIAL_MASK:
            return m.group(0)
        value = stored_values.get(m.group(1)[1:].lower())
        if not value:
            raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
        return m.group(1) + value
    resolved = _CREDENTIAL_QUERY_RE.sub(restore, resolved)
    return None if resolved == plain else resolved


def _get_lan_ip():
    """Auto-detect LAN IP for WebRTC ICE candidates."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def mjpeg_stream_name(printer_id: int) -> str:
    """go2rtc stream that serves a printer camera as MJPEG."""
    return f"printer_{printer_id}_mjpeg"


def go2rtc_config_for(printers, lan_ip) -> dict:
    """The one go2rtc config builder. ``printers`` are active, camera-enabled rows
    (ORM objects or anything with id/api_type/api_host/api_key/camera_url/model)."""
    streams = {}
    for p in printers:
        url = get_camera_url(p)
        if not url:
            continue
        # UniFi Protect: rtsps + port 7441 needs rtspx (no SRTP, skip cert check)
        if url.startswith("rtsps://") and ":7441" in url:
            url = "rtspx://" + url[8:]
            url = url.split("?enableSrtp")[0].split("&enableSrtp")[0]
        if url.startswith(("http://", "https://")):
            streams[f"printer_{p.id}"] = f"ffmpeg:{url}#video=h264"
            # MJPEG for <img> consumers (OBS overlay): the webcam's own stream.
            streams[mjpeg_stream_name(p.id)] = url
        else:
            streams[f"printer_{p.id}"] = url
            # go2rtc does not transcode H.264 for stream.mjpeg; an on-demand
            # ffmpeg variant (runs only while a consumer is attached) does.
            streams[mjpeg_stream_name(p.id)] = f"ffmpeg:printer_{p.id}#video=mjpeg"
    if not lan_ip:
        lan_ip = _get_lan_ip()
    webrtc_config = {"listen": "0.0.0.0:8555"}
    if lan_ip:
        webrtc_config["candidates"] = [f"{lan_ip}:8555"]
    return {
        "api": {"listen": "127.0.0.1:1984"},
        "webrtc": webrtc_config,
        "streams": streams,
    }


def _build_go2rtc_config(db: Session) -> dict:
    """Build go2rtc config dict from current printer state."""
    from modules.printers.models import Printer
    printers = db.query(Printer).filter(Printer.is_active.is_(True), Printer.camera_enabled.is_(True)).all()
    lan_ip = os.environ.get("ODIN_HOST_IP")
    if not lan_ip:
        row = db.execute(text("SELECT value FROM system_config WHERE key = 'host_ip'")).fetchone()
        if row:
            lan_ip = row[0]
    config = go2rtc_config_for(printers, lan_ip)
    db.commit()
    return config


_go2rtc_retry_lock = threading.Lock()
_go2rtc_retry_timers = {}
_go2rtc_pending_configs = {}
_go2rtc_retry_workers = {}


def _go2rtc_read_state(path):
    import json
    try:
        with open(path + '.sync-state.json') as stream:
            state = json.load(stream)
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _go2rtc_revision(state):
    value = state.get('intent_revision', 0)
    return value if type(value) is int and value >= 0 else 0


def _go2rtc_atomic_write(path: str, content: str):
    """Replace sensitive configuration/state without exposing a partial file."""
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix='.go2rtc-', dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _go2rtc_schedule_retry(path: str, lock_path: str, delay: float):
    """Coalesce local work; every worker reads latest disk state under flock."""
    with _go2rtc_retry_lock:
        if path in _go2rtc_retry_timers:
            return
        def retry():
            with _go2rtc_retry_lock:
                _go2rtc_retry_timers.pop(path, None)
                _go2rtc_retry_workers.setdefault(path, set()).add(threading.current_thread())
            try:
                _go2rtc_restart_with_lock(None, _path=path, _lock_path=lock_path)
            except Exception:
                log.warning('go2rtc deferred configuration sync failed')
                _go2rtc_schedule_retry(path, lock_path, max(_GO2RTC_MIN_RESTART_INTERVAL, 1.0))
            finally:
                with _go2rtc_retry_lock:
                    _go2rtc_retry_workers.get(path, set()).discard(threading.current_thread())
        timer = threading.Timer(max(delay, 0.01), retry)
        timer.daemon = True
        _go2rtc_retry_timers[path] = timer
        timer.start()


def _go2rtc_restart_with_lock(config: Optional[dict], *, force: bool = False, _path=None, _lock_path=None):
    """Persist desired configuration and eventually restart the latest version.

    A credential-free sidecar tracks the last successful fingerprint and restart
    attempt. Startup sync reconciles unfinished work after process replacement.
    """
    import hashlib
    import json
    import subprocess
    import math
    path = _path or GO2RTC_CONFIG
    lock_path = _lock_path or _GO2RTC_LOCKFILE
    lockfd = None
    registered = False
    initial_revision = None
    try:
        initial_revision = _go2rtc_revision(_go2rtc_read_state(path))
        lockfd = open(lock_path, 'a')
        fcntl.flock(lockfd, fcntl.LOCK_EX)
        state_path = path + '.sync-state.json'
        state = _go2rtc_read_state(path)
        revision = _go2rtc_revision(state)
        with _go2rtc_retry_lock:
            if config is not None:
                _go2rtc_pending_configs[path] = (config, revision)
                registered = True
        if config is not None:
            # Every explicit request advances intent, including unchanged/ABA values.
            state['intent_revision'] = revision + 1
            _go2rtc_atomic_write(state_path, json.dumps(state))
            revision += 1
            with _go2rtc_retry_lock:
                _go2rtc_pending_configs[path] = (config, revision)
        with _go2rtc_retry_lock:
            pending = _go2rtc_pending_configs.get(path)
        try:
            with open(path) as stream:
                desired = yaml.safe_load(stream)
        except (FileNotFoundError, yaml.YAMLError):
            desired = None
        if pending is not None and pending[1] != revision:
            # Another process accepted a newer request after our failed write.
            with _go2rtc_retry_lock:
                _go2rtc_pending_configs.pop(path, None)
            pending = None
        if pending is not None:
            pending = pending[0]
            if desired != pending:
                _go2rtc_atomic_write(path, yaml.safe_dump(pending))
            desired = pending
            with _go2rtc_retry_lock:
                _go2rtc_pending_configs.pop(path, None)
        if desired is None:
            return
        os.chmod(path, 0o600)
        fingerprint = hashlib.sha256(yaml.safe_dump(desired, sort_keys=True).encode()).hexdigest()
        if state.get('applied') == fingerprint and not force:
            return
        last_attempt = state.get('last_attempt')
        if isinstance(last_attempt, bool) or not isinstance(last_attempt, (int, float)) or not math.isfinite(last_attempt):
            last_attempt = None
        now = time.time()
        if last_attempt is not None and last_attempt > now:
            last_attempt = now
            state['last_attempt'] = now
            _go2rtc_atomic_write(state_path, json.dumps(state))
        remaining = 0 if last_attempt is None else min(_GO2RTC_MIN_RESTART_INTERVAL, max(0, _GO2RTC_MIN_RESTART_INTERVAL - (now - last_attempt)))
        if remaining and not force:
            _go2rtc_schedule_retry(path, lock_path, remaining)
            return
        state['last_attempt'] = now
        _go2rtc_atomic_write(state_path, json.dumps(state))
        try:
            result = subprocess.run(['supervisorctl', 'restart', 'go2rtc'], capture_output=True, timeout=5)
            succeeded = result.returncode == 0
        except Exception:
            succeeded = False
        if succeeded:
            state['applied'] = fingerprint
            _go2rtc_atomic_write(state_path, json.dumps(state))
            log.info('go2rtc restarted with current configuration')
        else:
            log.warning('go2rtc restart failed; configuration remains pending')
            _go2rtc_schedule_retry(path, lock_path, max(_GO2RTC_MIN_RESTART_INTERVAL, 0.01))
    except Exception:
        # A one-shot request must survive transient config/state persistence failure.
        if config is not None and not registered:
            with _go2rtc_retry_lock:
                _go2rtc_pending_configs.setdefault(path, (config, initial_revision))
        log.warning('go2rtc configuration sync remains pending')
        _go2rtc_schedule_retry(path, lock_path, max(_GO2RTC_MIN_RESTART_INTERVAL, 0.01))
    finally:
        if lockfd is not None:
            fcntl.flock(lockfd, fcntl.LOCK_UN)
            lockfd.close()


def sync_go2rtc_config(db: Session, *, force: bool = False):
    """Regenerate go2rtc config from printer camera URLs.

    Only restarts go2rtc if the config actually changed or if force=True.
    Protected by a cross-process file lock and cooldown to prevent
    restart storms when multiple cameras trigger sync simultaneously.
    """
    config = _build_go2rtc_config(db)
    with _go2rtc_thread_lock:
        _go2rtc_restart_with_lock(config, force=force)


def sync_go2rtc_config_standalone():
    """Regenerate go2rtc config (callable without a DB session)."""
    from core.db import SessionLocal
    db = SessionLocal()
    try:
        sync_go2rtc_config(db)
    finally:
        db.close()


def sync_go2rtc_config_raw():
    """Regenerate go2rtc config using raw SQL (safe to call from monitors)."""
    from types import SimpleNamespace
    from core.db_utils import get_db
    try:
        with get_db() as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT id, api_type, api_host, api_key, camera_url, model "
                "FROM printers WHERE is_active IS TRUE AND camera_enabled IS TRUE ORDER BY id"
            ).fetchall()
            printers = [SimpleNamespace(id=r[0], api_type=r[1], api_host=r[2], api_key=r[3], camera_url=r[4], model=r[5])
                        for r in rows]
            lan_ip = os.environ.get("ODIN_HOST_IP")
            if not lan_ip:
                r = cur.execute("SELECT value FROM system_config WHERE key = 'host_ip'").fetchone()
                if r:
                    lan_ip = r[0]
    except Exception as e:
        log.error(f"go2rtc raw sync DB error: {e}")
        return
    config = go2rtc_config_for(printers, lan_ip)
    _go2rtc_restart_with_lock(config)


# ====================================================================
# Printer command helpers
# ====================================================================

_ALLOWED_COMMANDS = frozenset({"pause_print", "resume_print", "stop_print", "cancel_print"})


def _call_adapter_method(adapter, action: str, *args):
    """Dispatch an allowed command to an adapter via explicit allowlist."""
    if action not in _ALLOWED_COMMANDS:
        raise ValueError(f"Unknown printer command: {action}")
    method = getattr(adapter, action, None)
    if method is None:
        raise ValueError(f"Adapter {type(adapter).__name__} does not support '{action}'")
    return method(*args)


def _bambu_command(printer, action: str) -> bool:
    """Send a command to a Bambu printer via a short-lived MQTT connection."""
    from modules.printers.telemetry.feature_flag import is_v2_enabled

    if is_v2_enabled():
        return _bambu_command_v2(printer, action)
    return _bambu_command_legacy(printer, action)


def _bambu_command_v2(printer, action: str) -> bool:
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.broker_policy import resolve_bambu_broker_config
    from modules.printers.telemetry.bambu.session import run_command
    try:
        creds = crypto.decrypt(printer.api_key)
        serial, access_code = creds.split("|", 1)
        endpoint = resolve_bambu_broker_config(
            printer.api_host, printer_id=f"cmd-{printer.id}",
        )
        config = BambuAdapterConfig(
            printer_id=f"cmd-{printer.id}",
            serial=serial,
            host=endpoint.host,
            port=endpoint.port,
            use_tls=endpoint.use_tls,
            access_code=access_code,
        )
        # V2's command adapter supports the same allowlisted methods.
        return run_command(config, action)
    except Exception as e:
        log.error(f"Bambu V2 {action} failed for printer {printer.id}: {e}")
        return False


def _bambu_command_legacy(printer, action: str) -> bool:
    from modules.printers.adapters.bambu import BambuPrinter
    import time as _time
    try:
        creds = crypto.decrypt(printer.api_key)
        serial, access_code = creds.split("|", 1)
        adapter = BambuPrinter(
            printer.api_host, serial, access_code,
            client_id=f"odin_cmd_{printer.id}_{int(_time.time())}"
        )
        if adapter.connect():
            success = _call_adapter_method(adapter, action)
            _time.sleep(0.3)
            adapter.disconnect()
            return success
    except Exception as e:
        log.error(f"Bambu {action} failed for printer {printer.id}: {e}")
    return False


def _prusalink_command(printer, action: str) -> bool:
    """Send a command to a PrusaLink printer."""
    from modules.printers.adapters.prusalink import PrusaLinkPrinter
    try:
        decrypted = crypto.decrypt(printer.api_key) if printer.api_key else ""
        if "|" in decrypted:
            username, password = decrypted.split("|", 1)
            adapter = PrusaLinkPrinter(printer.api_host, username=username, password=password)
        else:
            adapter = PrusaLinkPrinter(printer.api_host, api_key=decrypted)
        status = adapter.get_status()
        if not status or not status.job_id:
            log.error(f"PrusaLink {action}: no active job_id for printer {printer.id}")
            return False
        return _call_adapter_method(adapter, action, status.job_id)
    except Exception as e:
        log.error(f"PrusaLink {action} failed for printer {printer.id}: {e}")
    return False


def _elegoo_command(printer, action: str) -> bool:
    """Send a command to an Elegoo printer."""
    from modules.printers.adapters.elegoo import ElegooPrinter
    try:
        mainboard_id = crypto.decrypt(printer.api_key) if printer.api_key else ""
        adapter = ElegooPrinter(printer.api_host, mainboard_id=mainboard_id)
        if adapter.connect():
            success = _call_adapter_method(adapter, action)
            adapter.disconnect()
            return success
    except Exception as e:
        log.error(f"Elegoo {action} failed for printer {printer.id}: {e}")
    return False


def _send_printer_command(printer, action: str) -> bool:
    """Route a command to the correct adapter based on printer type."""
    if action not in _ALLOWED_COMMANDS:
        log.error(f"Rejected unknown printer command: {action}")
        return False
    if printer.api_type == "moonraker":
        from modules.printers.adapters.moonraker import MoonrakerPrinter
        adapter = MoonrakerPrinter(printer.api_host)
        return _call_adapter_method(adapter, action)
    elif printer.api_type == "prusalink":
        return _prusalink_command(printer, action)
    elif printer.api_type == "elegoo":
        return _elegoo_command(printer, action)
    else:
        return _bambu_command(printer, action)


def _bambu_command_direct(printer, method_name: str, *args, **kwargs) -> bool:
    """Call a BambuPrinter method directly (for commands not in the generic allowlist)."""
    from modules.printers.telemetry.feature_flag import is_v2_enabled

    if is_v2_enabled():
        from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
        from modules.printers.telemetry.bambu.broker_policy import resolve_bambu_broker_config
        from modules.printers.telemetry.bambu.session import run_command
        try:
            creds = crypto.decrypt(printer.api_key)
            serial, access_code = creds.split("|", 1)
            endpoint = resolve_bambu_broker_config(
                printer.api_host, printer_id=f"cmd-direct-{printer.id}",
            )
            config = BambuAdapterConfig(
                printer_id=f"cmd-direct-{printer.id}",
                serial=serial,
                host=endpoint.host,
                port=endpoint.port,
                use_tls=endpoint.use_tls,
                access_code=access_code,
            )
            return run_command(config, method_name, *args, **kwargs)
        except Exception as e:
            log.error(f"Bambu V2 {method_name} failed for printer {printer.id}: {e}")
            return False

    from modules.printers.adapters.bambu import BambuPrinter
    import time as _time
    try:
        creds = crypto.decrypt(printer.api_key)
        serial, access_code = creds.split("|", 1)
        adapter = BambuPrinter(
            printer.api_host, serial, access_code,
            client_id=f"odin_cmd_{printer.id}_{int(_time.time())}"
        )
        if adapter.connect():
            method = getattr(adapter, method_name, None)
            if method is None:
                return False
            success = method(*args, **kwargs)
            _time.sleep(0.3)
            adapter.disconnect()
            return success
    except Exception as e:
        log.error(f"Bambu {method_name} failed for printer {printer.id}: {e}")
    return False
