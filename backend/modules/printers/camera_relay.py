"""Internal pinned relay for HTTP/MJPEG printer cameras (go2rtc -> ffmpeg -> ODIN).

go2rtc hands HTTP camera sources to ffmpeg, which resolves names itself and
follows redirects. Instead of the camera URL, the go2rtc config names this
relay; ffmpeg therefore only ever talks to ODIN, and ODIN fetches the camera:

- through the LAN device destination policy (core.webhook_utils.device_request):
  host resolved and validated, connection pinned to the validated address,
  no redirects, no environment proxies;
- only the printer's stored HTTP(S) camera URL, re-read per request;
- bytes are streamed back; the relay never emits a redirect.

Access: loopback peers only (no forwarding headers) and a per-printer token,
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
_FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
_CHUNK = 64 * 1024
_secret_lock = threading.Lock()
_process_secret = None


def _secret_path() -> str:
    explicit = os.environ.get("ODIN_CAMERA_RELAY_SECRET_FILE")
    if explicit:
        return explicit
    from modules.printers.route_utils import GO2RTC_CONFIG
    return os.path.join(os.path.dirname(GO2RTC_CONFIG) or ".", ".camera-relay-secret")


def rotate_relay_secret() -> str:
    """New per-boot secret (API start-up). Falls back to a process secret if unwritable."""
    global _process_secret
    value = secrets.token_hex(32)
    path = _secret_path()
    with _secret_lock:
        try:
            tmp = f"{path}.{os.getpid()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(value)
            os.replace(tmp, path)
        except OSError:
            log.warning("Camera relay secret file not writable; using a process-local secret")
            _process_secret = value
        return value


def relay_secret() -> str:
    """Current per-boot secret, shared by API and monitor processes through its file."""
    global _process_secret
    try:
        with open(_secret_path()) as handle:
            value = handle.read().strip()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    if _process_secret is not None:
        return _process_secret
    value = rotate_relay_secret()
    return _process_secret if _process_secret is not None else value


def relay_token(printer_id: int) -> str:
    return hmac.new(relay_secret().encode(), f"camera-source:{int(printer_id)}".encode(), hashlib.sha256).hexdigest()


def relay_url(printer_id: int) -> str:
    """Loopback URL go2rtc/ffmpeg use for this printer's HTTP camera."""
    port = int(os.environ.get("PORT", "8000"))
    return f"http://127.0.0.1:{port}/internal/camera-source/{int(printer_id)}/{relay_token(printer_id)}"


def _is_local_peer(request: Request) -> bool:
    client = request.client.host if request.client else None
    if client not in _LOOPBACK_PEERS:
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

    def body():
        try:
            for chunk in upstream.iter_content(_CHUNK):
                if chunk:
                    yield chunk
        except requests.RequestException:
            return
        finally:
            upstream.close()

    return StreamingResponse(body(), status_code=200, media_type=content_type,
                             headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
