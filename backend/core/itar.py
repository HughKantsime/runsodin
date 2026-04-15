"""ITAR / CMMC air-gap enforcement.

Activated by the env var `ODIN_ITAR_MODE=1`. When enabled:

1. Container refuses to boot if any configured outbound destination
   (license_server_url, webhook URLs in `system_config`) resolves to
   a public address. Private/RFC1918/loopback are allowed — many ITAR
   shops run internal webhooks to their own logging/ticketing systems.

2. Runtime outbound HTTP (`safe_post`, `trusted_post` in
   core/webhook_utils.py) refuses public destinations at call time.
   Private destinations pass through normally.

3. This is stricter than the SSRF guard: SSRF tries to prevent ODIN
   from being weaponized to hit internal services (attacker-controlled
   URL → internal host). ITAR mode is the dual: prevent ODIN from
   leaking data to external services at all.

The two checks can coexist because they're layered: SSRF protects
against attackers supplying a private URL; ITAR protects against
operators (or admins) accidentally configuring a public URL when they
shouldn't be able to reach public destinations at all.

Env-var toggle (not a DB setting) because ITAR posture is a
container-level decision that cannot be flipped by a compromised
admin account. Operators set it in their compose / systemd unit and
a misconfiguration fails loud at boot.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from typing import Iterable, Optional
from urllib.parse import urlparse

log = logging.getLogger("odin.itar")

_ENABLED_VALUES = {"1", "true", "yes", "on"}


def is_itar_mode() -> bool:
    """Read the env var every call so tests can flip it without restarts."""
    return os.getenv("ODIN_ITAR_MODE", "").strip().lower() in _ENABLED_VALUES


def is_private_destination(host: str) -> bool:
    """True iff `host` is an RFC1918/loopback/link-local literal or
    resolves to one.

    Hostnames that don't resolve (transient DNS failure) are treated
    as non-private — the conservative call in ITAR mode is to block
    anything we can't positively identify as internal.
    """
    if not host:
        return False

    # Literal IP short-circuit.
    try:
        addr = ipaddress.ip_address(host)
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_unspecified
        )
    except ValueError:
        pass

    # Hostname lookup. Use getaddrinfo so we see every resolved address.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False

    resolved_addrs = {info[4][0] for info in infos}
    if not resolved_addrs:
        return False

    for addr_str in resolved_addrs:
        try:
            addr = ipaddress.ip_address(addr_str)
        except ValueError:
            continue
        if not (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_unspecified
        ):
            return False  # any public address disqualifies

    return True


def check_url_allowed(url: Optional[str]) -> tuple[bool, str]:
    """Return (allowed, reason). Empty / unset URLs are allowed.

    Used by the boot-time config audit and the runtime HTTP-client
    guard. A URL is allowed iff:
    - Empty / None (unconfigured), OR
    - The host resolves to a private/loopback address only.
    """
    if not url:
        return True, "unset"

    try:
        parsed = urlparse(url)
    except Exception:
        return False, f"malformed URL: {url!r}"

    host = parsed.hostname
    if not host:
        return False, f"URL has no host: {url!r}"

    if is_private_destination(host):
        return True, f"{host} resolves to a private range"
    return False, f"{host} resolves to a public address"


def audit_boot_config(urls: Iterable[str]) -> list[str]:
    """Inspect a list of configured URLs for ITAR compliance.

    Returns a list of violations (strings). Empty list means clean boot.
    Caller is responsible for refusing to start if the list is non-empty.
    """
    violations: list[str] = []
    for url in urls:
        ok, reason = check_url_allowed(url)
        if not ok:
            violations.append(f"  - {url} — {reason}")
    return violations


def enforce_boot_config(urls: Iterable[str]) -> None:
    """Raise RuntimeError with a clear message if ITAR config is violated.

    No-op if ITAR mode is off. Call this once in the app factory before
    accepting traffic so a misconfigured container fails immediately
    rather than leaking the first outbound request.
    """
    if not is_itar_mode():
        return

    violations = audit_boot_config(urls)
    if violations:
        msg = (
            "ODIN_ITAR_MODE=1 is set but the following outbound "
            "destinations resolve to public addresses:\n"
            + "\n".join(violations)
            + "\n\nFix the configured URLs or unset ODIN_ITAR_MODE."
        )
        log.critical(msg)
        raise RuntimeError(msg)
    log.info("ODIN_ITAR_MODE=1 boot audit passed — all configured URLs are private.")


class ItarOutboundBlocked(Exception):
    """Raised by the HTTP client guard when a public destination is
    attempted in ITAR mode."""


def enforce_request_destination(url: str) -> None:
    """Raise ItarOutboundBlocked if ITAR is on and the URL is public.

    Called by `safe_post` / `trusted_post` at the top of each request.
    No-op if ITAR mode is off.
    """
    if not is_itar_mode():
        return

    ok, reason = check_url_allowed(url)
    if not ok:
        raise ItarOutboundBlocked(
            f"ODIN_ITAR_MODE=1 refused outbound request — {reason}. "
            f"URL={url!r}."
        )
