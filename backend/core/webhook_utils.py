"""
O.D.I.N. — Webhook URL validation helper.

Provides SSRF-safe webhook URL validation used by the notifications
and organizations modules.

Two-stage validation:
  * _validate_webhook_url() — called at configuration time. Rejects
    literal private/loopback/link-local addresses and non-http(s) schemes.
  * resolve_and_check_webhook_url() — called at dispatch time (R8 from
    2026-04-12 adversarial review). DNS-resolves the hostname and rejects
    if ANY resolved address is private. Must be called on every dispatch,
    without caching, to defeat DNS rebinding and split-horizon DNS.
"""

import ipaddress
import socket
import urllib.parse

from fastapi import HTTPException


class WebhookSSRFError(Exception):
    """Raised when a webhook URL resolves to a disallowed (private) address.

    Use this instead of HTTPException in dispatch code paths so the caller
    can decide whether to surface a 400, 502, or just log and drop — the
    correct response depends on whether the webhook was just configured
    (surface 400 to admin) or is firing on a real event (502 + alert).
    """


_BLOCKED_HOSTNAME_PREFIXES = ("localhost", "127.", "169.254.", "0.", "::1")


def _is_disallowed_address(addr_str: str) -> bool:
    """Return True if the given IP string is in a disallowed range."""
    try:
        addr = ipaddress.ip_address(addr_str)
    except ValueError:
        return False
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _validate_webhook_url(url: str) -> None:
    """Validate a webhook URL is not targeting internal infrastructure (SSRF prevention).

    Allows http:// and https:// schemes only.
    Rejects loopback, link-local, and RFC-1918 private addresses (literal only).
    Raises HTTPException 400 if the URL is invalid or targets a blocked host.

    Note: this check does NOT resolve hostnames. A hostname that resolves
    to a private address (DNS rebinding, internal-DNS override) passes this
    gate. The dispatch-time gate resolve_and_check_webhook_url() is what
    actually prevents SSRF for hostname-based URLs.
    """
    if not url:
        return

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook URL")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Webhook URL must use http:// or https:// scheme")

    host = parsed.hostname or ""
    if any(host.startswith(p) for p in _BLOCKED_HOSTNAME_PREFIXES):
        raise HTTPException(status_code=400, detail="Webhook URL targets a blocked host")

    if _is_disallowed_address(host):
        raise HTTPException(status_code=400, detail="Webhook URL targets a blocked host")


def resolve_and_check_webhook_url(url: str) -> str:
    """Resolve a webhook URL's hostname and reject if any address is private.

    This is the dispatch-time SSRF gate (R8 from 2026-04-12 adversarial
    review). _validate_webhook_url() blocks LITERAL private IPs at config
    time, but a hostname like ``hooks.example.com`` could resolve to
    ``10.0.0.1`` — either via DNS rebinding (attacker-controlled zone) or
    split-horizon DNS (internal resolver shadows the public name).

    Call this immediately before every httpx.post/get — do NOT cache the
    result. DNS records change; a webhook that was safe 5 minutes ago can
    become an SSRF primitive now.

    Returns the original URL unchanged if all resolved addresses are public.
    Raises WebhookSSRFError if the hostname cannot resolve, or if any
    resolved address is in a disallowed range.
    """
    if not url:
        raise WebhookSSRFError("Empty webhook URL")

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        raise WebhookSSRFError(f"Webhook URL has no hostname: {url!r}")

    # Literal IP — use the already-loaded address
    try:
        addr = ipaddress.ip_address(host)
        if _is_disallowed_address(str(addr)):
            raise WebhookSSRFError(
                f"Webhook URL targets a private/reserved address: {host}"
            )
        return url
    except ValueError:
        pass  # hostname — proceed to DNS resolution

    # Hostname — resolve ALL addresses (v4 + v6) and check each one.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise WebhookSSRFError(f"Webhook hostname {host!r} could not be resolved: {e}")

    disallowed = []
    for family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        if _is_disallowed_address(ip_str):
            disallowed.append(ip_str)

    if disallowed:
        raise WebhookSSRFError(
            f"Webhook hostname {host!r} resolves to private/reserved "
            f"address(es) {disallowed}; refusing to dispatch"
        )

    return url
