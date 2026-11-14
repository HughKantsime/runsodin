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
import logging
import os
from typing import NamedTuple
from urllib.parse import urlsplit

from fastapi import HTTPException

from core.itar import ItarOutboundBlocked, is_itar_mode, pinned_literal_url, should_trust_env

log = logging.getLogger("odin.go2rtc")

DEFAULT_API_URL = "http://127.0.0.1:1984"
API_USERNAME = "odin"


ITAR_REFUSAL = ("Cameras are turned off: ITAR mode is on and ODIN_GO2RTC_API_URL points outside "
                "the local network.")


class Go2rtcUrlRefused(ItarOutboundBlocked, HTTPException):
    """ITAR mode is on and the go2rtc address is not loopback or on the local network."""

    def __init__(self):
        HTTPException.__init__(self, 409, ITAR_REFUSAL)


ITAR_NOT_RESTARTED = ("Cameras are turned off: ITAR mode is on and go2rtc has not yet been restarted "
                      "with the checked camera configuration.")


class Go2rtcNotChecked(Go2rtcUrlRefused):
    """ITAR mode is on, go2rtc is its own service, and it has not (yet) been shown to run
    the configuration ODIN checked (modules.printers.go2rtc_gate). Nothing is sent to it."""

    def __init__(self):
        HTTPException.__init__(self, 409, ITAR_NOT_RESTARTED)


class Go2rtcTarget(NamedTuple):
    """Where one go2rtc request goes, from one check of the address."""
    url: str             # base URL to send to
    client_kwargs: dict  # httpx keyword arguments for the client or the call
    remote: bool         # go2rtc is a separate service (not on this container's loopback)


def _is_loopback_host(host) -> bool:
    if not host:
        return True
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _checked_address():
    """(base URL, Host header or None, remote). Every go2rtc client gets its address here,
    so this is where ITAR mode is enforced.

    Under ITAR mode a URL that is not loopback or on the local network is refused and no
    client is handed it. A name is looked up once and the URL returned carries the address
    that passed (core.itar.pinned_literal_url), so the client never looks the name up
    again; the name goes in the Host header. With ITAR mode off the URL is returned as
    configured and nothing is looked up.
    """
    url = (os.environ.get("ODIN_GO2RTC_API_URL") or DEFAULT_API_URL).strip().rstrip("/")
    try:
        remote = not _is_loopback_host(urlsplit(url).hostname)
        pinned, host = pinned_literal_url(url)
        if host is not None and urlsplit(url).scheme.lower() == "https":
            # The certificate names the host and the connection goes to its address.
            raise ItarOutboundBlocked(
                "https to a go2rtc host name is not supported in ITAR mode; use http on the "
                "internal network, or the IP address")
    except (ItarOutboundBlocked, ValueError) as exc:
        if not is_itar_mode():
            raise
        log.error("go2rtc address refused; no request was sent: %s", exc)
        raise Go2rtcUrlRefused() from None
    return pinned, host, remote


def go2rtc_target(_gate: bool = True) -> Go2rtcTarget:
    """The address and httpx settings for a request to go2rtc's API, from one check.

    Under ITAR mode with go2rtc as its own service, the caller gets a target only once
    go2rtc has been restarted with the checked configuration (go2rtc_gate.require_gate, which
    does that on the first contact); otherwise Go2rtcNotChecked is raised and the caller
    sends nothing. `_gate=False` is for the gate's own two requests.

    Callers build the request from this one value: `target.url` plus the path, and
    `**target.client_kwargs` on the client (or on httpx.get/post/stream).
    With ITAR mode off the settings are the per-boot credentials off loopback and nothing on
    loopback. Under ITAR mode they also turn environment proxies off (trust_env) and, when
    the URL names a host, carry that name as the Host header.
    """
    url, host, remote = _checked_address()
    kwargs = {"auth": go2rtc_api_credentials()} if remote else {}
    if is_itar_mode():
        kwargs["trust_env"] = should_trust_env()
        if host is not None:
            kwargs["headers"] = {"Host": host}
        if remote and _gate:
            from modules.printers import go2rtc_gate
            try:
                go2rtc_gate.require_gate()
            except (go2rtc_gate.Go2rtcNotRunning, go2rtc_gate.Go2rtcGateFailed):
                raise Go2rtcNotChecked() from None
    return Go2rtcTarget(url, kwargs, remote)


def go2rtc_api_url() -> str:
    """The go2rtc API base URL (see go2rtc_target, which request code uses)."""
    return _checked_address()[0]


def go2rtc_api_host() -> str:
    return urlsplit(go2rtc_api_url()).hostname or "127.0.0.1"


def go2rtc_api_port() -> int:
    return urlsplit(go2rtc_api_url()).port or 1984


def go2rtc_is_remote() -> bool:
    """True when go2rtc is a separate service (not on this container's loopback)."""
    return _checked_address()[2]


def go2rtc_api_listen() -> str:
    """go2rtc's api.listen: loopback beside the API, the service network otherwise."""
    url, _host, remote = _checked_address()
    port = urlsplit(url).port or 1984
    return f":{port}" if remote else f"127.0.0.1:{port}"


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
    """httpx keyword arguments for a request to go2rtc's API (see go2rtc_target)."""
    return go2rtc_target().client_kwargs
