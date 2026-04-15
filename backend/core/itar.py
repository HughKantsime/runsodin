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


def collect_db_configured_urls() -> list[str]:
    """Pull every DB-backed outbound URL that would be called at runtime.

    Codex pass 4 (2026-04-14) flagged that the startup audit only
    inspected statically-known settings (license_server_url). A
    freshly-installed ODIN with ITAR=1 could boot clean and then leak
    data on the first webhook dispatch because the runtime
    enforce_request_destination check is reactive.

    This function consolidates every DB source of outbound URLs so
    the boot audit can fail closed:
      - `webhooks` table: per-alert dispatch targets (URL stored
        encrypted; decrypt for audit).
      - `system_config` rows with keys that historically carry a URL
        (`license_server_url` — set dynamically), leaving room for
        future entries.

    Returns a flat list of plaintext URL strings. Silently drops
    rows that fail to decrypt (logs a warning) — a boot audit
    shouldn't crash on corrupt data, but a malformed row can't be
    validated either.
    """
    from core.db import SessionLocal
    from sqlalchemy import text as sa_text

    urls: list[str] = []
    db = SessionLocal()
    try:
        # Webhook table (post-v1 notifications refactor).
        try:
            rows = db.execute(
                sa_text("SELECT url FROM webhooks")
            ).fetchall()
            for row in rows:
                raw = row[0] if row else None
                if not raw:
                    continue
                try:
                    # Match routes/webhooks.py decryption pattern.
                    from core.crypto import decrypt, is_encrypted
                    plain = decrypt(raw) if is_encrypted(raw) else raw
                except Exception as exc:
                    log.warning("ITAR audit: could not decrypt webhook url: %s", exc)
                    continue
                urls.append(plain)
        except Exception as exc:
            # Table may not exist on a fresh install before first
            # migration of the notifications module.
            log.debug("ITAR audit: webhooks table not queryable: %s", exc)

        # system_config rows that historically carry a URL value.
        try:
            rows = db.execute(
                sa_text(
                    "SELECT key, value FROM system_config "
                    "WHERE key IN ('license_server_url', 'update_server_url')"
                )
            ).fetchall()
            for key, value in rows:
                if not value:
                    continue
                urls.append(str(value))
        except Exception as exc:
            log.debug("ITAR audit: system_config not queryable: %s", exc)
    finally:
        db.close()

    return urls


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
