"""Internal pinned relay for HTTP/MJPEG printer cameras (go2rtc -> ffmpeg -> ODIN).

go2rtc hands HTTP camera sources to ffmpeg, which resolves names itself and
follows redirects. Instead of the camera URL, the go2rtc config names this
relay; ffmpeg therefore only ever talks to ODIN, and ODIN fetches the camera:

- through the LAN device destination policy (core.webhook_utils.device_request):
  host resolved and validated, connection pinned to the validated address,
  no redirects, no environment proxies;
- only the printer's stored HTTP(S) camera URL, re-read per request;
- bytes are streamed back; the relay never emits a redirect.

Access: loopback peers, or the configured relay peers (ODIN_CAMERA_RELAY_PEERS: the
go2rtc service when it runs as its own container), never with a proxy/forwarding
header, and always a per-printer token,
HMAC-SHA256 over the printer id with a per-boot random secret. The secret
lives in a 0600 file next to the go2rtc config, rotated at API start-up and
read by every process that writes the go2rtc config; only go2rtc's config
carries the resulting URLs. Any refusal is a 404 that reveals nothing.

Merge note: cert-fix-B's overlay relay (camera_routes.py, go2rtc MJPEG to
overlay viewers) streams in the opposite direction; the shared patterns
(single close path, content-type gate, no-store headers) are reimplemented
here so the branches stay independent.
"""
import hashlib
import hmac
import logging
import os
import secrets
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

log = logging.getLogger("odin.camera_relay")
router = APIRouter(include_in_schema=False)

RELAY_PATH = "/internal/camera-source/{printer_id}/{token}"
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1"})
# Any proxy/CDN marker means the request did not come straight from the relay consumer.
_FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "via",
                       "cf-connecting-ip", "true-client-ip", "x-client-ip")
_CHUNK = 64 * 1024
_secret_lock = threading.Lock()
_process_secret = None


def _secret_path() -> str:
    explicit = os.environ.get("ODIN_CAMERA_RELAY_SECRET_FILE")
    if explicit:
        return explicit
    from modules.printers.route_utils import GO2RTC_CONFIG
    return os.path.join(os.path.dirname(GO2RTC_CONFIG) or ".", ".camera-relay-secret")


class RelaySecretUnavailable(RuntimeError):
    """No camera relay secret yet, in a process that must not create one (fails closed)."""


def _may_rotate() -> bool:
    """Only the API process owns the secret (ODIN_DB_ROLE api, or unset before supervisord
    in the entrypoint and in local runs). Monitors, vision, timelapse and reports read it."""
    return os.environ.get("ODIN_DB_ROLE", "api") in ("", "api")


def _read_secret(path: str) -> str | None:
    try:
        with open(path) as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value if len(value) >= 32 else None


def rotate_relay_secret() -> str:
    """New per-boot secret (API start-up), at most once per boot.

    Under an exclusive lock beside the secret: when ODIN_BOOT_ID (exported once by the
    container entrypoint) matches the boot marker and a secret exists, that secret is
    kept, so a failed pre-start step cannot make every uvicorn worker rotate again.
    Falls back to a process secret if the file is unwritable."""
    import fcntl
    global _process_secret
    path = _secret_path()
    boot = (os.environ.get("ODIN_BOOT_ID") or "").strip()
    with _secret_lock:
        lock = None
        try:
            lock = open(f"{path}.lock", "a")
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            lock = None
        try:
            current = _read_secret(path)
            if boot and current:
                try:
                    with open(f"{path}.boot") as marker:
                        if marker.read().strip() == boot:
                            return current  # already rotated in this boot
                except OSError:
                    pass
            value = secrets.token_hex(32)
            try:
                # Keep the previous boot's secret: a go2rtc service that outlives an API
                # restart still runs with credentials derived from it until it is reloaded.
                if current:
                    tmp_previous = f"{path}.previous.{os.getpid()}.tmp"
                    fd = os.open(tmp_previous, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as handle:
                        handle.write(current)
                    os.replace(tmp_previous, f"{path}.previous")
                tmp = f"{path}.{os.getpid()}.tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(value)
                os.replace(tmp, path)
                if boot:
                    tmp_marker = f"{path}.boot.{os.getpid()}.tmp"
                    fd = os.open(tmp_marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as handle:
                        handle.write(boot)
                    os.replace(tmp_marker, f"{path}.boot")
            except OSError:
                log.warning("Camera relay secret file not writable; using a process-local secret")
                _process_secret = value
            return value
        finally:
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()


def relay_secret() -> str:
    """Current per-boot secret, shared by API and monitor processes through its file.
    Only the API process creates a missing secret; any other process fails closed."""
    value = _read_secret(_secret_path())
    if value:
        return value
    if _process_secret is not None:
        return _process_secret
    if not _may_rotate():
        raise RelaySecretUnavailable("camera relay secret not created yet (the API owns it)")
    value = rotate_relay_secret()
    return _process_secret if _process_secret is not None else value


def previous_relay_secret():
    """The secret before the last rotation, or None."""
    try:
        with open(f"{_secret_path()}.previous") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value if len(value) >= 32 else None


def relay_token(printer_id: int) -> str:
    return hmac.new(relay_secret().encode(), f"camera-source:{int(printer_id)}".encode(), hashlib.sha256).hexdigest()


def relay_base_url() -> str:
    """Where go2rtc reaches the relay: loopback in the single container; the API service
    (ODIN_CAMERA_RELAY_BASE_URL, e.g. http://api:8000) when go2rtc is its own container."""
    configured = (os.environ.get("ODIN_CAMERA_RELAY_BASE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    port = int(os.environ.get("PORT", "8000"))
    return f"http://127.0.0.1:{port}"


def relay_url(printer_id: int) -> str:
    """URL go2rtc/ffmpeg use for this printer's HTTP camera."""
    return f"{relay_base_url()}/internal/camera-source/{int(printer_id)}/{relay_token(printer_id)}"


def _trusted_peers() -> set:
    """Loopback plus ODIN_CAMERA_RELAY_PEERS (comma-separated IPs or host names, e.g. the
    go2rtc service), resolved at check time. The HMAC token is required in every case."""
    import ipaddress
    import socket
    peers = set(_LOOPBACK_PEERS)
    for entry in (os.environ.get("ODIN_CAMERA_RELAY_PEERS") or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            peers.add(str(ipaddress.ip_address(entry)))
            continue
        except ValueError:
            pass
        try:
            for info in socket.getaddrinfo(entry, None, type=socket.SOCK_STREAM):
                peers.add(info[4][0])
        except (socket.gaierror, OSError, UnicodeError):
            log.warning("Camera relay peer %r could not be resolved", entry)
    return peers


def _is_local_peer(request: Request) -> bool:
    client = request.client.host if request.client else None
    if client is None:
        return False
    if client not in _LOOPBACK_PEERS and client not in _trusted_peers():
        return False
    return not any(request.headers.get(name) for name in _FORWARDING_HEADERS)


def _stored_http_camera_url(printer_id: int):
    import core.crypto as crypto
    from core.db_utils import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT camera_url FROM printers WHERE id = ? AND is_active IS TRUE AND camera_enabled IS TRUE",
            (printer_id,),
        ).fetchone()
    if not row or not row[0]:
        return None
    url = crypto.decrypt(row[0]) if crypto.is_encrypted(row[0]) else row[0]
    if not isinstance(url, str) or url.split("://", 1)[0].lower() not in ("http", "https"):
        return None
    return url


_SNIFF_BYTES = 64 * 1024


def _body_matches(content_type: str, head: bytes) -> bool:
    """The body really is what the camera declared: a multipart stream starts with its
    boundary delimiter, an image with a JPEG start-of-image marker. Anything else (an HLS
    playlist, HTML, another container format) is refused before ffmpeg sees a byte."""
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    if kind.startswith("multipart/"):
        import re
        match = re.search(r'(?i)boundary="?([^";\s]+)"?', content_type or "")
        stripped = head.lstrip(b"\r\n")
        if not stripped.startswith(b"--"):
            return False
        if match:
            boundary = match.group(1).encode("latin-1", "replace")
            if boundary.startswith(b"--"):
                boundary = boundary[2:]
            return stripped[2:].startswith(boundary) or stripped.startswith(boundary)
        return True
    return head.startswith(b"\xff\xd8")


def _allowed_content_type(value: str) -> bool:
    kind = (value or "").split(";", 1)[0].strip().lower()
    return kind.startswith("multipart/") or kind.startswith("image/")


@router.get(RELAY_PATH)
def camera_source(printer_id: int, token: str, request: Request):
    if not _is_local_peer(request) or not hmac.compare_digest(token, relay_token(printer_id)):
        raise HTTPException(status_code=404)
    url = _stored_http_camera_url(printer_id)
    if url is None:
        raise HTTPException(status_code=404)
    import requests
    from core.webhook_utils import DeviceDestinationBlocked, device_request
    try:
        upstream = device_request("GET", url, stream=True, timeout=(5, 30))
    except (DeviceDestinationBlocked, requests.RequestException):
        log.warning("Camera relay fetch refused or failed for printer %s", printer_id)
        raise HTTPException(status_code=502)
    content_type = upstream.headers.get("Content-Type", "")
    if upstream.status_code != 200 or not _allowed_content_type(content_type):
        # Redirects (3xx) and anything else are never passed on.
        upstream.close()
        raise HTTPException(status_code=502)

    chunks = upstream.iter_content(_CHUNK)
    head = b""
    try:
        while len(head) < _SNIFF_BYTES:
            chunk = next(chunks, None)
            if chunk is None:
                break
            head += chunk
            if len(head) >= 256 or head.startswith(b"\xff\xd8"):
                break
    except requests.RequestException:
        upstream.close()
        raise HTTPException(status_code=502)
    if not _body_matches(content_type, head):
        upstream.close()
        log.warning("Camera relay refused printer %s: body is not the declared MJPEG/JPEG", printer_id)
        raise HTTPException(status_code=502)

    def body():
        try:
            if head:
                yield head
            for chunk in chunks:
                if chunk:
                    yield chunk
        except requests.RequestException:
            return
        finally:
            upstream.close()

    return StreamingResponse(body(), status_code=200, media_type=content_type,
                             headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
