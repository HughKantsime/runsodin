"""Where ODIN reaches go2rtc's API, and with which per-boot credentials.

Single container (default): go2rtc runs beside the API, its API listens on loopback and
supervisord restarts it. Enterprise compose: go2rtc is its own service, reached at
ODIN_GO2RTC_API_URL (e.g. http://go2rtc:1984) on the compose network. Its API then
listens on the container's interfaces (never published) and requires the per-boot
credentials below; ODIN reloads it through POST /api/restart.

Credentials are derived from the camera relay's per-boot secret, like go2rtc's RTSP
credentials, so every ODIN process that shares the secret file computes the same values.
go2rtc does not ask loopback clients for them, so in the single container requests carry
none.
"""
import hashlib
import hmac
import ipaddress
import os
from urllib.parse import urlsplit

DEFAULT_API_URL = "http://127.0.0.1:1984"
API_USERNAME = "odin"


def go2rtc_api_url() -> str:
    return (os.environ.get("ODIN_GO2RTC_API_URL") or DEFAULT_API_URL).strip().rstrip("/")


def _is_loopback_host(host) -> bool:
    if not host:
        return True
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def go2rtc_api_host() -> str:
    return urlsplit(go2rtc_api_url()).hostname or "127.0.0.1"


def go2rtc_api_port() -> int:
    return urlsplit(go2rtc_api_url()).port or 1984


def go2rtc_is_remote() -> bool:
    """True when go2rtc is a separate service (not on this container's loopback)."""
    return not _is_loopback_host(urlsplit(go2rtc_api_url()).hostname)


def go2rtc_api_listen() -> str:
    """go2rtc's api.listen: loopback beside the API, the service network otherwise."""
    port = go2rtc_api_port()
    return f":{port}" if go2rtc_is_remote() else f"127.0.0.1:{port}"


def _derive(secret: str, purpose: bytes) -> str:
    return hmac.new(secret.encode(), purpose, hashlib.sha256).hexdigest()


def go2rtc_api_credentials(secret=None):
    from modules.printers.camera_relay import relay_secret
    return API_USERNAME, _derive(secret if secret is not None else relay_secret(), b"go2rtc-api")


def go2rtc_restart_credential_candidates():
    """Credentials go2rtc may be running with: this boot's, then the previous boot's
    (go2rtc keeps the configuration it last loaded until it is reloaded)."""
    from modules.printers.camera_relay import previous_relay_secret, relay_secret
    candidates = [go2rtc_api_credentials(relay_secret())]
    previous = previous_relay_secret()
    if previous:
        older = go2rtc_api_credentials(previous)
        if older not in candidates:
            candidates.append(older)
    return candidates


def go2rtc_client_kwargs() -> dict:
    """httpx keyword arguments for a request to go2rtc's API ({} on loopback)."""
    return {"auth": go2rtc_api_credentials()} if go2rtc_is_remote() else {}
