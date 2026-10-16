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
    """Config-time device destination policy (printers, plugs, cameras).

    Delegates to core.webhook_utils.device_destination_ips: parses brackets
    and ports, resolves hostnames and numeric IPv4 forms, and rejects
    loopback, link-local, unspecified, multicast, metadata and IPv4-mapped
    forms of those. Private LAN ranges are allowed. Resolver errors are 400.
    """
    from core.webhook_utils import DeviceDestinationBlocked, device_destination_ips
    try:
        device_destination_ips(host)
    except DeviceDestinationBlocked:
        raise HTTPException(status_code=400, detail="Invalid printer host")


def pinned_device_get(client, url: str, **kwargs):
    """GET a device URL through the destination policy, pinned to its validated address.

    `client` is the httpx module or client; env proxies are bypassed so the
    pin cannot be sidestepped. A blocked destination raises
    core.webhook_utils.DeviceDestinationBlocked.
    """
    from core.webhook_utils import pinned_device_destination
    with pinned_device_destination(url):
        return client.get(url, trust_env=False, follow_redirects=False, **kwargs)


_CAMERA_SHELL_METACHAR_RE = re.compile(r'[;&|$`\\]')


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

    # Userinfo must be unambiguous: urlsplit ends the authority at the first
    # / ? #, so an unencoded @ / ? # in a password would make the parsed host
    # (and the masking) disagree with what the user meant.
    _prefix, userinfo, _rest = split_url_userinfo(sanitized)
    if userinfo is not None and any(ch in userinfo for ch in "@/?#"):
        raise HTTPException(status_code=400, detail=(
            "Camera URL username/password contains @ / ? or #; percent-encode them "
            "(%40 %2F %3F %23)"))
    try:
        parsed = urllib.parse.urlsplit(sanitized)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="Camera URL is malformed")
    if not host:
        raise HTTPException(status_code=400, detail="Camera URL has no host")
    try:
        _check_ssrf_blocklist(f"[{host}]" if ":" in host else host)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Camera URL points to a blocked host")

    return sanitized


# Bambu models with an RTSPS camera stream at :322/streaming/live/1 (auto
# camera URL). Bambu Studio's machine profiles carry no camera data, so a model
# is listed only with another real source; P2S, H2S, H2C and H2D Pro are NOT
# listed until verified on hardware (fixes.json records the claim text).
BAMBU_RTSPS_CAMERA_MODELS = frozenset({'X1C', 'X1 Carbon', 'X1E', 'X1 Carbon Combo', 'H2D'})


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
        RTSP_MODELS = BAMBU_RTSPS_CAMERA_MODELS
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


_URL_SCHEME_RE = re.compile(r'^([a-z][a-z0-9+.\-]*://)', re.IGNORECASE)
CAMERA_CREDENTIAL_MASK = '***'


_HOST_PORT_RE = re.compile(r'^(\[[0-9A-Fa-f:.%]+\]|[^\s:@\[\]/?#]+)(:[0-9]{0,5})?$')


def split_url_userinfo(url: str):
    """Split a URL into (scheme://, userinfo or None, text after the userinfo '@').

    Only the authority (up to the first / ? #, as urlsplit defines it) can
    carry userinfo, so an '@' in the path or query is never treated as a
    credential. The one exception is a legacy stored value whose unencoded
    / ? # in a password makes the authority not a valid host[:port] (e.g.
    "u:se/cret@host"): then everything up to the last '@' is treated as the
    credential, so masking never leaks part of it.
    """
    match = _URL_SCHEME_RE.match(url or '')
    if not match:
        return None, None, url
    rest = url[match.end():]
    end = min([i for i in (rest.find('/'), rest.find('?'), rest.find('#')) if i >= 0] or [len(rest)])
    authority = rest[:end]
    at = authority.rfind('@')
    if at >= 0:
        return match.group(1), rest[:at], rest[at + 1:]
    if '@' in rest and not _HOST_PORT_RE.match(authority):
        last = rest.rfind('@')
        return match.group(1), rest[:last], rest[last + 1:]
    return match.group(1), None, rest


def sanitize_camera_url(url: str) -> str:
    """Mask URL credentials for API responses, for any scheme and any case."""
    if not url:
        return url
    prefix, userinfo, rest = split_url_userinfo(url.strip())
    if userinfo is None:
        return url.strip()
    return prefix + CAMERA_CREDENTIAL_MASK + '@' + rest


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
    import urllib.parse
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme.lower(), (parsed.hostname or '').lower(), parsed.port)
    except ValueError:
        return None


def resolve_masked_camera_url(incoming, stored):
    """Restore credentials when a client sends back a masked camera URL.

    Returns the plaintext URL to save, or None when the client sent back
    exactly the masked form of the stored URL (nothing changed). Stored
    credentials are carried over only to the same scheme, host and port
    (a path change is allowed); pointing a masked URL at another endpoint,
    or sending a masked URL with no stored credentials, is a 400 that
    requires the full credentialed URL.
    """
    if not isinstance(incoming, str):
        return incoming
    prefix, userinfo, rest = split_url_userinfo(incoming.strip())
    if userinfo != CAMERA_CREDENTIAL_MASK:
        return incoming
    plain = crypto.decrypt(stored) if stored and crypto.is_encrypted(stored) else stored
    stored_prefix, stored_userinfo, stored_rest = split_url_userinfo((plain or '').strip())
    if stored_userinfo is None or crypto.is_encrypted(plain or ''):
        raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
    # Compare endpoints with the userinfo removed, so a legacy password with
    # an unencoded / ? # cannot shift the parsed host.
    target = _url_endpoint(prefix + rest)
    if target is None or target != _url_endpoint(stored_prefix + stored_rest):
        raise HTTPException(status_code=400,
                            detail="Camera URL endpoint changed; enter the full URL including credentials")
    resolved = prefix.lower() + stored_userinfo + '@' + rest
    return None if resolved == plain.strip() else resolved


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


GO2RTC_RTSP_SCHEMES = ("rtsp", "rtsps")


def go2rtc_stream_source(url):
    """go2rtc source for a camera URL, or None when it must not be streamed.

    - Only RTSP/RTSPS sources are handed to go2rtc. HTTP(S) sources would run
      through ffmpeg, which follows redirects outside the device policy, so
      they are not streamed (discovery no longer records them either).
    - The camera host is resolved and validated with the device destination
      policy, and the validated IP literal replaces the host in the source,
      so go2rtc never resolves a name itself (no rebinding). Userinfo, port,
      path and query are kept. go2rtc does not verify RTSPS certificates
      (Bambu printers and most cameras are self-signed), so no hostname is
      needed for TLS.
    - UniFi Protect rtsps :7441 becomes rtspx without enableSrtp, as before.
    """
    import urllib.parse
    from core.webhook_utils import DeviceDestinationBlocked, device_connect_ip
    if not isinstance(url, str) or not url.strip():
        return None
    prefix, userinfo, rest = split_url_userinfo(url.strip())
    if prefix is None or prefix[:-3].lower() not in GO2RTC_RTSP_SCHEMES:
        return None
    scheme = prefix[:-3].lower()
    end = min([i for i in (rest.find('/'), rest.find('?'), rest.find('#')) if i >= 0] or [len(rest)])
    try:
        parsed = urllib.parse.urlsplit(f"{scheme}://{rest[:end]}")
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if not host:
        return None
    try:
        address = device_connect_ip(host, port or (322 if scheme == "rtsps" else 554))
    except DeviceDestinationBlocked:
        log.warning("Camera source host blocked by device policy; stream omitted")
        return None
    shown = f"[{address}]" if ":" in address else address
    source = f"{scheme}://" + (f"{userinfo}@" if userinfo is not None else "") + shown + (f":{port}" if port else "") + rest[end:]
    if scheme == "rtsps" and port == 7441:
        source = "rtspx://" + source[len("rtsps://"):]
        source = source.split("?enableSrtp")[0].split("&enableSrtp")[0]
    return source


def _build_go2rtc_config(db: Session) -> dict:
    """Build go2rtc config dict from current printer state."""
    from modules.printers.models import Printer
    printers = db.query(Printer).filter(Printer.is_active.is_(True), Printer.camera_enabled.is_(True)).all()
    streams = {}
    for p in printers:
        source = go2rtc_stream_source(get_camera_url(p))
        if source:
            streams[f"printer_{p.id}"] = source
    db.commit()
    webrtc_config = {"listen": "0.0.0.0:8555"}
    lan_ip = os.environ.get("ODIN_HOST_IP")
    if not lan_ip:
        row = db.execute(text("SELECT value FROM system_config WHERE key = 'host_ip'")).fetchone()
        if row:
            lan_ip = row[0]
    if not lan_ip:
        lan_ip = _get_lan_ip()
    if lan_ip:
        webrtc_config["candidates"] = [f"{lan_ip}:8555"]
    return {
        "api": {"listen": "127.0.0.1:1984"},
        "webrtc": webrtc_config,
        "streams": streams,
    }


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
    from core.db_utils import get_db
    streams = {}
    try:
        with get_db() as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT id, api_type, api_host, api_key, camera_url, model "
                "FROM printers WHERE is_active IS TRUE AND camera_enabled IS TRUE"
            ).fetchall()
            for row in rows:
                pid, api_type, api_host, api_key, camera_url, model = row
                url = None
                if camera_url:
                    try:
                        url = crypto.decrypt(camera_url)
                    except Exception:
                        url = camera_url
                elif api_type == 'bambu' and api_key and api_host:
                    RTSP_MODELS = BAMBU_RTSPS_CAMERA_MODELS
                    if (model or '').strip() in RTSP_MODELS:
                        try:
                            parts = crypto.decrypt(api_key).split('|')
                            if len(parts) == 2:
                                url = f"rtsps://bblp:{urlquote(parts[1], safe='')}@{api_host}:322/streaming/live/1"
                        except Exception as e:
                            log.debug(f"Failed to decrypt Bambu key for go2rtc: {e}")
                source = go2rtc_stream_source(url)
                if source:
                    streams[f"printer_{pid}"] = source

            lan_ip = os.environ.get("ODIN_HOST_IP")
            if not lan_ip:
                r = cur.execute("SELECT value FROM system_config WHERE key = 'host_ip'").fetchone()
                if r:
                    lan_ip = r[0]
    except Exception as e:
        log.error(f"go2rtc raw sync DB error: {e}")
        return

    if not lan_ip:
        lan_ip = _get_lan_ip()
    webrtc_config = {"listen": "0.0.0.0:8555"}
    if lan_ip:
        webrtc_config["candidates"] = [f"{lan_ip}:8555"]
    config = {
        "api": {"listen": "127.0.0.1:1984"},
        "webrtc": webrtc_config,
        "streams": streams,
    }

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
