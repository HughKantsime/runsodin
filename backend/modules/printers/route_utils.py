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
from modules.printers.bambu_chamber_camera import (
    BAMBU_CHAMBER_CAMERA_MODELS, SOURCE as CHAMBER_CAMERA_SOURCE, chamber_source,
)

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
    api_key: Optional[str] = None


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


def prusalink_test_get(host: str, credential: Optional[str]):
    """Use the monitor's API-key / username|password convention, pinned to policy."""
    import httpx
    if credential and "|" in credential:
        username, password = credential.split("|", 1)
        options = {"auth": httpx.DigestAuth(username, password)}
    else:
        options = {"headers": {"X-Api-Key": credential}} if credential else {
            "auth": httpx.DigestAuth("maker", "")}
    return pinned_device_get(httpx, f"http://{host}/api/version", timeout=5, **options)


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
        # With the port: ODIN's own service ports on its own addresses are refused.
        _check_ssrf_blocklist((f"[{host}]" if ":" in host else host) + (f":{port}" if port else ""))
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
    """Stored/derived URL, or a credential-free chamber marker resolved only by the relay."""
    if printer.camera_url:
        url = printer.camera_url
        try:
            url = crypto.decrypt(url)
        except Exception:
            log.debug("Failed to decrypt camera URL (using raw)")
        return url
    if chamber_source(printer):
        return CHAMBER_CAMERA_SOURCE
    if printer.api_type == "bambu" and printer.api_key and printer.api_host:
        RTSP_MODELS = BAMBU_RTSPS_CAMERA_MODELS
        model = (printer.model or '').strip()
        if model not in RTSP_MODELS:
            return None
        try:
            parts = crypto.decrypt(printer.api_key).split("|")
            if len(parts) == 2:
                return f"rtsps://bblp:{urlquote(parts[1], safe='')}@{printer.api_host}:322/streaming/live/1"
        except Exception:
            log.debug("Failed to decrypt Bambu API key")
    return None


def effective_camera_url(printer):
    """Stored camera URL (possibly encrypted), else the one generated from the
    printer's credentials. Contains credentials: responses must mask it."""
    return printer.camera_url or get_camera_url(printer)


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


# Query parameters some cameras take credentials in (?user=..&password=..).
_CREDENTIAL_QUERY_RE = re.compile(r'([?&](?:user|username|usr|login|password|pass|passwd|pwd|token|auth|key|secret|api_key|apikey|access_code|sig)=)([^&#]*)', re.IGNORECASE)
# Vendors that put the credential in the path. UniFi Protect: rtsp://host:7447/<token>,
# rtsps://host:7441/<token>[?enableSrtp]. Other path-token schemes are not recognised
# and are stored as entered (documented limitation: use userinfo or query credentials).
_PATH_TOKEN_RE = re.compile(r'^(rtsps?://[^/?#]+:(?:7441|7447)/)([A-Za-z0-9]{8,})(?=$|[?#])', re.IGNORECASE)
_DEFAULT_PORTS = {'rtsp': 554, 'rtsps': 322, 'rtspx': 322, 'http': 80, 'https': 443}


def camera_url_has_credentials(url) -> bool:
    """Userinfo, a credential query parameter or a path token: the URL must be stored encrypted."""
    if not isinstance(url, str):
        return False
    return (split_url_userinfo(url.strip())[1] is not None
            or any(m.group(2) for m in _CREDENTIAL_QUERY_RE.finditer(url))
            or bool(_PATH_TOKEN_RE.match(url.strip())))


def sanitize_camera_url(url: str) -> str:
    """Mask URL credentials (userinfo and credential query values) for API responses."""
    if not url:
        return url
    prefix, userinfo, rest = split_url_userinfo(url.strip())
    masked = url.strip() if userinfo is None else prefix + CAMERA_CREDENTIAL_MASK + '@' + rest
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
    return split_url_userinfo(url.strip())[1] == CAMERA_CREDENTIAL_MASK or any(
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
    prefix, userinfo, rest = split_url_userinfo(incoming)
    stored_prefix, stored_userinfo, stored_rest = split_url_userinfo(plain)
    if prefix is None or stored_prefix is None:
        raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
    # Compare endpoints with the userinfo removed, so a legacy password with
    # an unencoded / ? # cannot shift the parsed host (default ports are equivalent).
    target = _url_endpoint(prefix + rest)
    if target is None or target != _url_endpoint(stored_prefix + stored_rest):
        raise HTTPException(status_code=400,
                            detail="Camera URL endpoint changed; enter the full URL including credentials")
    resolved = incoming
    if userinfo == CAMERA_CREDENTIAL_MASK:
        if stored_userinfo is None:
            raise HTTPException(status_code=400, detail="Camera URL credentials are masked; enter the full URL")
        resolved = prefix.lower() + stored_userinfo + '@' + rest
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


GO2RTC_RTSP_SCHEMES = ("rtsp", "rtsps")


def go2rtc_stream_source(url, printer_id=None):
    """go2rtc source for a camera URL, or None when it must not be streamed.

    - RTSP/RTSPS: the camera host is resolved and validated with the device
      destination policy, and the validated IP literal replaces it, so go2rtc
      never resolves a name itself (no rebinding). Userinfo, port, path and
      query are kept. go2rtc does not verify RTSPS certificates (Bambu printers
      and most cameras are self-signed), so no hostname is needed for TLS.
    - HTTP(S) (MJPEG/snapshot): go2rtc hands these to ffmpeg, which resolves
      and follows redirects itself. The source is therefore ffmpeg reading
      ODIN's loopback relay (modules.printers.camera_relay), which fetches the
      camera pinned, without redirects or proxies. Needs printer_id.
    - Chamber marker: the same relay, with policy checked on every upstream connection.
    - UniFi Protect rtsps :7441 becomes rtspx without enableSrtp, as before.
    """
    import urllib.parse
    from core.webhook_utils import DeviceDestinationBlocked, device_connect_ip
    if not isinstance(url, str) or not url.strip():
        return None
    if url == CHAMBER_CAMERA_SOURCE:
        if printer_id is None:
            return None
        from modules.printers.camera_relay import relay_url
        return f"ffmpeg:{relay_url(printer_id)}#video=h264"
    prefix, userinfo, rest = split_url_userinfo(url.strip())
    if prefix is not None and prefix[:-3].lower() in ("http", "https"):
        if printer_id is None:
            return None
        try:
            parsed = urllib.parse.urlsplit(url.strip())
            device_connect_ip(parsed.hostname or "", parsed.port)
        except (DeviceDestinationBlocked, ValueError):
            log.warning("Camera source host blocked by device policy; stream omitted")
            return None
        from modules.printers.camera_relay import relay_url
        return f"ffmpeg:{relay_url(printer_id)}#video=h264"
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


def mjpeg_stream_name(printer_id: int) -> str:
    """go2rtc stream that serves a printer camera as MJPEG."""
    return f"printer_{printer_id}_mjpeg"


GO2RTC_RTSP_LISTEN = "127.0.0.1:8554"


def go2rtc_rtsp_credentials():
    """Per-boot go2rtc RTSP credentials, derived from the camera relay's per-boot secret
    (shared by every process that writes the go2rtc config)."""
    import hashlib
    import hmac
    from modules.printers.camera_relay import relay_secret
    password = hmac.new(relay_secret().encode(), b"go2rtc-rtsp", hashlib.sha256).hexdigest()
    return "odin", password


def go2rtc_rtsp_url(stream_name: str) -> str:
    """ODIN's own authenticated loopback URL for a go2rtc stream."""
    user, password = go2rtc_rtsp_credentials()
    return f"rtsp://{user}:{password}@{GO2RTC_RTSP_LISTEN}/{stream_name}"


def go2rtc_config_for(printers, lan_ip) -> dict:
    """The one go2rtc config builder. ``printers`` are active, camera-enabled rows
    (ORM objects or anything with id/api_type/api_host/api_key/camera_url/model).

    Every source comes from go2rtc_stream_source (validated IP literal for RTSP,
    the pinned loopback relay for HTTP), so go2rtc and ffmpeg never resolve or
    fetch a camera name themselves. The MJPEG variant for <img> consumers (OBS
    overlay) is the relay itself for HTTP cameras (it passes multipart/image
    bodies through) and an on-demand ffmpeg transcode of the main stream for RTSP.
    """
    from modules.printers.go2rtc_access import go2rtc_api_credentials, go2rtc_api_listen, go2rtc_is_remote
    rtsp_user, rtsp_password = go2rtc_rtsp_credentials()
    api_user, api_password = go2rtc_api_credentials()
    streams = {}
    for p in printers:
        source = go2rtc_stream_source(get_camera_url(p), p.id)
        if not source:
            continue
        streams[f"printer_{p.id}"] = source
        if source.startswith("ffmpeg:"):
            from modules.printers.camera_relay import relay_url
            streams[mjpeg_stream_name(p.id)] = relay_url(p.id)
        else:
            # go2rtc does not transcode H.264 for stream.mjpeg; an on-demand
            # ffmpeg variant (runs only while a consumer is attached) reads the
            # main stream from go2rtc's own loopback RTSP server, with its
            # per-boot credentials.
            streams[mjpeg_stream_name(p.id)] = f"ffmpeg:{go2rtc_rtsp_url(f'printer_{p.id}')}#video=mjpeg"
    if not lan_ip and not go2rtc_is_remote():
        # Beside go2rtc, this container's outbound address is go2rtc's. When go2rtc is its
        # own service it never is, and each writer (api, monitors) would advertise its own
        # address: only ODIN_HOST_IP / system_config host_ip is used then.
        lan_ip = _get_lan_ip()
    webrtc_config = {"listen": "0.0.0.0:8555"}
    if lan_ip:
        webrtc_config["candidates"] = [f"{lan_ip}:8555"]
    return {
        # Loopback beside the API (single container); the compose network when go2rtc is
        # its own service (ODIN_GO2RTC_API_URL), never published. Off loopback go2rtc
        # requires these per-boot credentials, so no other container can read a stream.
        "api": {"listen": go2rtc_api_listen(), "username": api_user, "password": api_password},
        # go2rtc's RTSP server re-serves every printer stream: loopback only, and
        # authenticated with per-boot credentials only ODIN's own sources carry.
        "rtsp": {"listen": GO2RTC_RTSP_LISTEN, "username": rtsp_user, "password": rtsp_password},
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
        succeeded = _go2rtc_reload()
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


def _go2rtc_reload() -> bool:
    """Make go2rtc load the config file: supervisord beside the API, otherwise go2rtc's
    own POST /api/restart (reloads in place) with the credentials it is running with."""
    import subprocess
    from modules.printers.go2rtc_access import (
        go2rtc_api_url, go2rtc_is_remote, go2rtc_restart_credential_candidates,
    )
    if not go2rtc_is_remote():
        try:
            result = subprocess.run(['supervisorctl', 'restart', 'go2rtc'], capture_output=True, timeout=5)
            return result.returncode == 0
        except Exception:
            return False
    import httpx
    for auth in go2rtc_restart_credential_candidates():
        try:
            response = httpx.post(f"{go2rtc_api_url()}/api/restart", auth=auth, timeout=5)
        except httpx.HTTPError:
            return False
        if response.status_code == 200:
            return True
        if response.status_code != 401:
            return False
    log.warning('go2rtc refused every ODIN credential; restart the go2rtc service to load the current configuration')
    return False


def write_go2rtc_config_before_start(config: dict, *, _path=None) -> None:
    """Container start, before supervisord: write the config. In the single container it
    is recorded as applied, so go2rtc boots with this boot's credentials and relay tokens
    and the API's start-up sync finds nothing to restart."""
    import hashlib
    import json
    path = _path or GO2RTC_CONFIG
    content = yaml.safe_dump(config)
    _go2rtc_atomic_write(path, content)
    os.chmod(path, 0o600)
    from modules.printers.go2rtc_access import go2rtc_is_remote
    if go2rtc_is_remote():
        # A separate go2rtc service may already be running an older config: leave it
        # pending so the API's start-up sync reloads it.
        return
    state = _go2rtc_read_state(path)
    state['applied'] = hashlib.sha256(yaml.safe_dump(config, sort_keys=True).encode()).hexdigest()
    _go2rtc_atomic_write(path + '.sync-state.json', json.dumps(state))


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
    from modules.printers.camera_relay import RelaySecretUnavailable
    try:
        config = go2rtc_config_for(printers, lan_ip)
    except RelaySecretUnavailable:
        log.error("go2rtc raw sync skipped: the camera relay secret has not been created by the API yet")
        return
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
    if printer.api_type == "anycubic_lan":
        return False  # Monitor-only: never fall through to the Bambu command path.
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


def test_anycubic_lan_connection(host):
    """Safe, credential-free response for the authenticated/setup connection probe."""
    from modules.printers.adapters.anycubic_lan import AnycubicLANPrinter
    from modules.printers.printer_models import normalize_model_name
    client = AnycubicLANPrinter(host)
    try:
        if not client.connect():
            return {"success": False, "error": "Connection failed. Check IP and enable LAN mode."}
        if not client.poll():
            return {"success": False, "error": "Anycubic LAN query failed."}
        deadline = time.monotonic() + 5
        while not client.last_info and time.monotonic() < deadline:
            time.sleep(0.1)
        if not client.last_info:
            return {"success": False, "error": "No Anycubic LAN info report received."}
        status = client.get_status()
        from modules.printers.parsing.anycubic_lan import filament_slots
        return {"success": True, "state": status.internal_state,
                "bed_temp": status.bed_temp, "nozzle_temp": status.nozzle_temp,
                "ams_slots": len(filament_slots(status.boxes)),
                "model": normalize_model_name("anycubic_lan", client.model_id)}
    except Exception:
        return {"success": False, "error": "Anycubic LAN connection failed."}
    finally:
        client.disconnect()
