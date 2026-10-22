"""The one Spoolman base-URL validator and HTTP client (review O1).

Applied when the URL is saved (PUT /config) and on every Spoolman call (GET and PUT).
The base URL must be http(s)://host[:port][/path] with no userinfo, query or fragment.
Its host is resolved and every address must pass the device destination policy
(core.webhook_utils: no link-local, metadata, multicast, unspecified or reserved address)
with one difference (review O1-b): loopback is allowed, because Spoolman beside a
bare-metal or host-network ODIN install (http://localhost:7912) is a supported setup.
No address may be one of ODIN's own service endpoints (is_own_service_endpoint, and
loopback on any of ODIN's own service ports); in ITAR mode it must be private.
Requests are pinned to the validated addresses, never follow redirects and ignore
environment proxies.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


class SpoolmanURLRefused(ValueError):
    """The configured Spoolman URL is not an allowed destination."""


def check_spoolman_url_shape(url: str) -> str:
    """Structural checks only (no DNS): the normalized base URL, or SpoolmanURLRefused."""
    value = (url or "").strip()
    try:
        parsed = urlsplit(value)
        parsed.port  # noqa: B018 - raises on an invalid port
    except ValueError:
        raise SpoolmanURLRefused("Invalid Spoolman URL") from None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise SpoolmanURLRefused("Spoolman URL must be http(s)://host[:port]")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise SpoolmanURLRefused("Spoolman URL must not contain credentials")
    if parsed.query or parsed.fragment or "?" in value or "#" in value:
        raise SpoolmanURLRefused("Spoolman URL must not contain a query string or fragment")
    return value.rstrip("/")


def _check_itar(addresses) -> None:
    from core.itar import is_itar_mode
    if is_itar_mode():
        for address in addresses:
            if not ipaddress.ip_address(str(address).split("%", 1)[0]).is_private:
                raise SpoolmanURLRefused("Spoolman destination is public in ITAR mode")


def validate_spoolman_base(url: str) -> str:
    """Full validation including resolution (used when the URL is saved): the normalized
    base URL, or SpoolmanURLRefused."""
    base = check_spoolman_url_shape(url)
    _destination(base)
    return base


def _is_loopback(address) -> bool:
    from core.webhook_utils import _embedded_ipv4
    addr = ipaddress.ip_address(str(address).split("%", 1)[0])
    embedded = _embedded_ipv4(addr)
    return addr.is_loopback or (embedded is not None and embedded.is_loopback)


def spoolman_destination_ips(hostname: str, port: int) -> list:
    """The validated addresses of a Spoolman host: the device policy with loopback
    allowed, minus ODIN's own service endpoints. Raises SpoolmanURLRefused."""
    import socket
    from core import webhook_utils
    try:
        name = webhook_utils.normalize_hostname(hostname)
        infos = webhook_utils._real_getaddrinfo(name, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise SpoolmanURLRefused(f"Spoolman destination refused: host {hostname!r} could not be resolved") from None
    addresses = []
    for *_ignored, sockaddr in infos:
        ip = sockaddr[0]
        try:
            loopback = _is_loopback(ip)
        except ValueError:
            raise SpoolmanURLRefused("Spoolman destination refused: invalid address") from None
        if loopback:
            # Loopback is always one of ODIN's own addresses.
            if port in webhook_utils.own_service_ports():
                raise SpoolmanURLRefused(f"Spoolman destination refused: port {port} is one of ODIN's own services")
        elif webhook_utils.is_blocked_device_address(ip):
            raise SpoolmanURLRefused(f"Spoolman destination refused: host {hostname!r} resolves to a blocked address")
        if webhook_utils.is_own_service_endpoint(ip, port):
            raise SpoolmanURLRefused(f"Spoolman destination refused: port {port} is one of ODIN's own services")
        if ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise SpoolmanURLRefused(f"Spoolman destination refused: host {hostname!r} returned no addresses")
    _check_itar(addresses)
    return addresses


def _destination(base: str):
    parsed = urlsplit(base)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.hostname, port, spoolman_destination_ips(parsed.hostname, port)


def spoolman_request(method: str, path: str, *, url: str | None = None, timeout: float = 10, **kwargs):
    """One validated, pinned, redirect-free request to the configured Spoolman.

    The host is resolved once (core.webhook_utils.pinned_device_destination: device policy
    and ODIN's own endpoints) and the connection is pinned to those addresses; a 3xx is
    returned as the response, never followed; environment proxies are ignored."""
    import requests
    from core.spoolman_settings import get_spoolman_url
    from core.webhook_utils import _pin_dns
    base = check_spoolman_url_shape(url if url is not None else get_spoolman_url())
    if not path.startswith("/"):
        raise ValueError("Spoolman path must be absolute")
    kwargs.pop("allow_redirects", None)
    kwargs.pop("proxies", None)
    hostname, port, addresses = _destination(base)
    with requests.Session() as session:
        session.trust_env = False
        session.proxies = {}
        with _pin_dns(hostname, port, addresses):
            return session.request(method, base + path, timeout=timeout, allow_redirects=False, **kwargs)


def spool_summary(spool: dict) -> dict:
    """Only the spool fields ODIN uses (never the raw upstream body)."""
    filament = spool.get("filament") if isinstance(spool.get("filament"), dict) else {}
    vendor = filament.get("vendor") if isinstance(filament.get("vendor"), dict) else {}
    return {
        "id": spool.get("id"),
        "remaining_weight": spool.get("remaining_weight"),
        "filament": {
            "name": filament.get("name"),
            "material": filament.get("material"),
            "color_name": filament.get("color_name"),
            "color_hex": filament.get("color_hex"),
            "vendor": {"name": vendor.get("name")},
        },
    }


def filament_summary(filament: dict) -> dict:
    """Only the filament fields ODIN exposes."""
    vendor = filament.get("vendor") if isinstance(filament.get("vendor"), dict) else {}
    return {
        "id": filament.get("id"),
        "name": filament.get("name"),
        "vendor": {"name": vendor.get("name")},
        "material": filament.get("material"),
        "color_hex": filament.get("color_hex"),
        "diameter": filament.get("diameter"),
        "density": filament.get("density"),
        "weight": filament.get("weight"),
    }


def fetch_spools(timeout: float = 10) -> list[dict]:
    """GET /api/v1/spool from the configured Spoolman, projected to spool_summary."""
    response = spoolman_request("GET", "/api/v1/spool", timeout=timeout)
    response.raise_for_status()
    data = response.json()
    return [spool_summary(s) for s in data if isinstance(s, dict)] if isinstance(data, list) else []


def spoolman_healthy(timeout: float = 5) -> bool:
    try:
        return spoolman_request("GET", "/api/v1/health", timeout=timeout).status_code == 200
    except Exception:
        return False
