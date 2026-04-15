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
    """Pull every DB-backed outbound destination that would be called at runtime.

    Codex pass 4 + pass 6 (2026-04-14/15): the ITAR boot audit must
    cover every real egress path, not just `webhooks`. This function
    now consolidates:
      - `webhooks` table: per-alert dispatch targets (URL stored
        encrypted; decrypt for audit).
      - `system_config` URL-bearing rows (`license_server_url`,
        `update_server_url`).
      - `system_config.smtp_config` (host + port → audit as
        smtp://host:port).
      - `system_config` MQTT republish host (mqtt://host:port).
      - `groups.settings_json` webhook URLs (org-level outbound from
        OrgSettingsProvider).

    Non-HTTP destinations (SMTP, MQTT) are encoded as synthetic URLs
    so `check_url_allowed` (which uses urlparse + getaddrinfo on the
    hostname) can validate them uniformly. The audit only cares about
    whether the host resolves to RFC1918/loopback; scheme / port are
    not used for the network check.

    Returns a flat list of plaintext URL strings. Silently drops rows
    that fail to decrypt or parse — a boot audit shouldn't crash on
    corrupt data, but a malformed row can't be validated either so
    it's best-effort.
    """
    from core.db import SessionLocal
    from sqlalchemy import text as sa_text

    urls: list[str] = []
    db = SessionLocal()
    try:
        # -------- 1. webhooks table (encrypted) --------
        try:
            rows = db.execute(sa_text("SELECT url FROM webhooks")).fetchall()
            for row in rows:
                raw = row[0] if row else None
                if not raw:
                    continue
                try:
                    from core.crypto import decrypt, is_encrypted
                    plain = decrypt(raw) if is_encrypted(raw) else raw
                except Exception as exc:
                    log.warning("ITAR audit: could not decrypt webhook url: %s", exc)
                    continue
                urls.append(plain)
        except Exception as exc:
            log.debug("ITAR audit: webhooks table not queryable: %s", exc)

        # -------- 2. system_config URL-bearing keys --------
        try:
            rows = db.execute(
                sa_text(
                    "SELECT key, value FROM system_config "
                    "WHERE key IN ('license_server_url', 'update_server_url', "
                    "              'smtp_config', "
                    "              'mqtt_republish_host', 'mqtt_republish_port')"
                )
            ).fetchall()
            mqtt_host: Optional[str] = None
            mqtt_port: Optional[str] = None
            for key, value in rows:
                if not value:
                    continue
                if key in ("license_server_url", "update_server_url"):
                    urls.append(str(value))
                elif key == "smtp_config":
                    # JSON blob; extract host + port.
                    try:
                        import json as _json
                        cfg = (
                            _json.loads(value) if isinstance(value, str) else value
                        )
                        host = cfg.get("host")
                        port = cfg.get("port", 25)
                        enabled = cfg.get("enabled", True)
                        if enabled and host:
                            urls.append(f"smtp://{host}:{int(port)}")
                    except Exception as exc:
                        log.warning("ITAR audit: could not parse smtp_config: %s", exc)
                elif key == "mqtt_republish_host":
                    mqtt_host = str(value)
                elif key == "mqtt_republish_port":
                    mqtt_port = str(value)
            if mqtt_host:
                port_s = mqtt_port or "1883"
                urls.append(f"mqtt://{mqtt_host}:{port_s}")
        except Exception as exc:
            log.debug("ITAR audit: system_config not queryable: %s", exc)

        # -------- 3. groups.settings_json — org-level webhooks --------
        try:
            rows = db.execute(
                sa_text("SELECT id, settings_json FROM groups")
            ).fetchall()
            import json as _json
            for row in rows:
                raw = row[1] if len(row) > 1 else None
                if not raw:
                    continue
                try:
                    settings = _json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    continue
                # Org webhook shape varies; check the common keys that
                # can hold a URL.
                for k in ("webhook_url", "notification_webhook_url",
                          "alert_webhook_url", "digest_webhook_url"):
                    v = settings.get(k) if isinstance(settings, dict) else None
                    if v:
                        urls.append(str(v))
        except Exception as exc:
            log.debug("ITAR audit: groups table not queryable: %s", exc)
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


def enforce_host_destination(host: str, scheme: str = "net") -> None:
    """Raise ItarOutboundBlocked if ITAR is on and `host` is public.

    For non-HTTP outbound channels — SMTP (smtplib), MQTT (paho), any
    socket-level connect. The resolver check is the same as
    enforce_request_destination but takes a bare hostname rather
    than a URL.

    Codex pass 7 (2026-04-15): boot-time DNS audit alone isn't enough.
    A host that resolved private at boot could resolve public later
    under split-horizon DNS / config drift / resolver changes. Every
    outbound client path that could leak data must check on each
    connect.

    `scheme` is just a label threaded into the error message for
    operator debugging ("smtp", "mqtt", etc.).
    """
    if not is_itar_mode():
        return
    if not host:
        return
    if is_private_destination(host):
        return
    raise ItarOutboundBlocked(
        f"ODIN_ITAR_MODE=1 refused {scheme} connect — "
        f"{host!r} resolves to a public address."
    )
