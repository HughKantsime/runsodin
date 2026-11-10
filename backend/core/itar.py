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

Identity-provider allowance (`ODIN_ITAR_ALLOWED_IDP_HOSTS`)
-----------------------------------------------------------
A comma-separated list of exact DNS hostnames (max 8, no wildcards, no IPs in
any form). Env-only, like ITAR mode itself. Under ITAR mode it lets the OIDC
client, and only the OIDC client, reach those hosts over https/443 even though
they resolve to public addresses. The OIDC client marks its requests with
`oidc_request_scope()`; `pin_for_request` reads that ContextVar. No function
signature carries the marker, so every other caller is unchanged and cannot
opt in by accident. With the env var unset, or ITAR mode off, behaviour is
identical to a build without the allowance.
"""

from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import logging
import os
import re
import socket
from typing import Iterable, Optional
from urllib.parse import urlparse

log = logging.getLogger("odin.itar")

_ENABLED_VALUES = {"1", "true", "yes", "on"}
_LOCAL_V4 = tuple(ipaddress.ip_network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
))
_LOCAL_V6 = tuple(ipaddress.ip_network(cidr) for cidr in ("fc00::/7", "::1/128", "fe80::/10"))

IDP_HOSTS_ENV = "ODIN_ITAR_ALLOWED_IDP_HOSTS"
_MAX_IDP_HOSTS = 8
_IDP_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_OIDC_KINDS = frozenset({"oidc", "discovery", "token", "userinfo", "jwks"})

# Set only by oidc_request_scope(). The value is the endpoint kind (used for the
# audit row). contextvars.copy_context().run, used by core.outbound_http to
# start its worker, carries it into the worker thread.
_oidc_scope: contextvars.ContextVar = contextvars.ContextVar("odin_itar_oidc_scope", default=None)


def _is_local_address(addr) -> bool:
    """Only RFC1918/ULA/loopback/link-local, including mapped IPv4.

    ipaddress.is_private also covers special-use space that can carry public
    IPv4 traffic. Teredo, 6to4 and both NAT64 prefixes are deliberately excluded,
    even with private embedded IPv4; so are unspecified and documentation IPs.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    networks = _LOCAL_V4 if addr.version == 4 else _LOCAL_V6
    return any(addr in network for network in networks)


def is_itar_mode() -> bool:
    """Read the env var every call so tests can flip it without restarts."""
    return os.getenv("ODIN_ITAR_MODE", "").strip().lower() in _ENABLED_VALUES


def _parses_as_ip(entry: str) -> bool:
    """True if `entry` is an address under ipaddress or the C resolver.

    socket.inet_aton accepts short, hexadecimal and octal IPv4 forms ("8.8",
    "0x8.8.8.8", "010.1.1.1", "127.1") that ipaddress rejects but getaddrinfo
    resolves as addresses.
    """
    try:
        ipaddress.ip_address(entry.strip("[]"))
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(entry)
        return True
    except (OSError, ValueError):
        return False


def _is_dns_hostname(entry: str) -> bool:
    """Proper DNS name: 2+ LDH labels, TLD has a letter and is not all digits."""
    if len(entry) > 253:
        return False
    labels = entry.split(".")
    if len(labels) < 2 or not all(_IDP_LABEL.fullmatch(label) for label in labels):
        return False
    tld = labels[-1]
    return not tld.isdigit() and any(ch.isalpha() for ch in tld)


def parse_allowed_idp_hosts(raw: Optional[str]) -> tuple[tuple[str, ...], list[str]]:
    """Parse the allowance. Returns (hosts, errors).

    Entries are trimmed and lowercased. Each must be an exact DNS name: at least
    two labels, each 1-63 LDH characters (letters, digits, hyphen; no leading or
    trailing hyphen), and a final label that has a letter and is not all digits.
    No wildcard, IP literal in any form the resolver accepts (dotted, short,
    hexadecimal, octal, IPv6), scheme, port, path or trailing dot. At most 8
    distinct hosts.
    """
    hosts: list[str] = []
    errors: list[str] = []
    for part in (raw or "").split(","):
        entry = part.strip().lower()
        if not entry:
            continue
        if "*" in entry:
            errors.append(f"{entry!r}: wildcards are not allowed")
            continue
        if _parses_as_ip(entry):
            errors.append(f"{entry!r}: IP addresses are not allowed")
            continue
        if not _is_dns_hostname(entry):
            errors.append(
                f"{entry!r}: must be an exact hostname "
                "(no scheme, port, path or trailing dot)"
            )
            continue
        if entry not in hosts:
            hosts.append(entry)
    if len(hosts) > _MAX_IDP_HOSTS:
        errors.append(f"{len(hosts)} hosts listed; the maximum is {_MAX_IDP_HOSTS}")
    return tuple(hosts), errors


def idp_allowance_errors() -> list[str]:
    """Problems with the env var value (empty when unset or valid)."""
    raw = os.getenv(IDP_HOSTS_ENV, "")
    if not raw.strip():
        return []
    return parse_allowed_idp_hosts(raw)[1]


def allowed_idp_hosts() -> frozenset:
    """The effective allowance. Empty when unset, and empty (fail closed) when
    the value is invalid; the boot audit refuses to start in that case."""
    raw = os.getenv(IDP_HOSTS_ENV, "")
    if not raw.strip():
        return frozenset()
    hosts, errors = parse_allowed_idp_hosts(raw)
    return frozenset() if errors else frozenset(hosts)


@contextlib.contextmanager
def oidc_request_scope(kind: str = "oidc"):
    """Mark outbound requests made inside this block as OIDC-client requests.

    Only `modules/organizations/oidc_handler.py` may use this; a contract test
    enforces it. `pin_for_request` reads the marker. `kind` is the endpoint
    label recorded in the audit row: discovery, token, userinfo or jwks.
    """
    if kind not in _OIDC_KINDS:
        raise ValueError(f"unknown OIDC request kind: {kind!r}")
    token = _oidc_scope.set(kind)
    try:
        yield
    finally:
        _oidc_scope.reset(token)


def idp_discovery_url_allowed(url: Optional[str]) -> bool:
    """True iff `url` is https/443 on a host in the allowance (boot audit use)."""
    allowed = allowed_idp_hosts()
    if not allowed or not url:
        return False
    try:
        parsed = urlparse(str(url))
        port = parsed.port or 443
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and port == 443
        and (parsed.hostname or "").lower() in allowed
    )


def enforce_idp_endpoint_hosts(discovery_url: Optional[str], config) -> None:
    """Refuse a discovery document that pulls in a host outside the allowance.

    Applies only under ITAR mode, with a non-empty allowance, when the discovery
    host itself is on the allowance. A private IdP (discovery host not on the
    allowance) keeps the normal private-only guard and is not touched here.
    """
    if not is_itar_mode():
        return
    allowed = allowed_idp_hosts()
    if not allowed:
        return
    try:
        discovery_host = (urlparse(str(discovery_url or "")).hostname or "").lower()
    except ValueError:
        return
    if discovery_host not in allowed:
        return
    for key in ("token_endpoint", "userinfo_endpoint", "jwks_uri"):
        value = config.get(key)
        if value is None:
            continue
        try:
            host = (urlparse(str(value)).hostname or "").lower()
        except ValueError:
            host = ""
        if host not in allowed:
            raise ItarOutboundBlocked(
                f"ODIN_ITAR_MODE=1: OIDC discovery {key} host {host or str(value)!r} "
                f"is not in {IDP_HOSTS_ENV}; refusing"
            )


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
        return _is_local_address(addr)
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
            return False
        if not _is_local_address(addr):
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

    allowance_errors = idp_allowance_errors()
    if allowance_errors:
        msg = (
            f"ODIN_ITAR_MODE=1 is set but {IDP_HOSTS_ENV} is invalid:\n"
            + "\n".join(f"  - {err}" for err in allowance_errors)
            + f"\n\nFix {IDP_HOSTS_ENV} or unset it."
        )
        log.critical(msg)
        raise RuntimeError(msg)

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
    allowance = sorted(allowed_idp_hosts())
    if allowance:
        log.info(
            "ODIN_ITAR_MODE=1 boot audit passed — all configured URLs are private "
            "or on the identity-provider allowance (%s=%s).",
            IDP_HOSTS_ENV, ",".join(allowance),
        )
    else:
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
      - enabled `oidc_config.discovery_url`, except an https/443 URL on a
        host in the identity-provider allowance.

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

        # -------- 3. oidc_config.discovery_url (only when enabled) --------
        # Codex pass 15 (2026-04-15): OIDC login unconditionally
        # reaches out to discovery_url; under ITAR it must be private
        # or the service should fail at boot rather than at first login.
        # An https/443 URL on an explicitly allowed IdP host is the one
        # exception; anything else public stays in the list and fails the audit.
        try:
            rows = db.execute(
                sa_text(
                    "SELECT discovery_url FROM oidc_config "
                    "WHERE is_enabled IS TRUE AND discovery_url IS NOT NULL"
                )
            ).fetchall()
            for row in rows:
                url = row[0] if row else None
                if url and not idp_discovery_url_allowed(str(url)):
                    urls.append(str(url))
        except Exception as exc:
            log.debug("ITAR audit: oidc_config not queryable: %s", exc)

        # -------- 4. groups.settings_json — org-level webhooks --------
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


def should_trust_env() -> bool:
    """Decide whether httpx should honor HTTP(S)_PROXY env vars.

    Codex pass 14 (2026-04-15): an earlier fix forced
    `trust_env=False` on every ITAR-protected httpx client to prevent
    a proxy from bypassing the DNS pin. But outside ITAR mode,
    operators often rely on a corporate proxy for legitimate egress
    (OIDC discovery, license server, Spoolman). Hard-coding
    trust_env=False broke those deployments with no security benefit
    — pin_for_request is itself a no-op outside ITAR mode.

    Returns True (honor proxies) when ITAR is OFF, False (disable
    proxies) when ITAR is ON. Every ITAR-protected httpx client
    should pass `trust_env=should_trust_env()`.
    """
    return not is_itar_mode()


def _record_idp_outbound(host: str, kind: str) -> None:
    """Write the `itar.idp_outbound` audit row (host and endpoint kind only,
    never a token or URL path). Fails closed: if the row cannot be committed
    the request is refused."""
    try:
        from core.audit import log_audit
        from core.db import SessionLocal
        db = SessionLocal()
        try:
            log_audit(db, "itar.idp_outbound", "oidc", None,
                      {"host": host, "endpoint": kind})
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        raise ItarOutboundBlocked(
            f"ODIN_ITAR_MODE=1: could not write the audit row for an allowed "
            f"identity-provider request to {host!r}; refusing"
        ) from exc


def _require_idp_transport(host: str, scheme: str, port: int) -> None:
    if scheme != "https" or port != 443:
        raise ItarOutboundBlocked(
            f"ODIN_ITAR_MODE=1: {host!r} is an allowed identity-provider host "
            f"but only https on port 443 is permitted (got {scheme}:{port})"
        )


@contextlib.contextmanager
def pin_for_request(url: str):
    """Context manager: validate AND DNS-pin an outbound httpx call.

    Codex pass 11 (2026-04-15): the bare `enforce_request_destination`
    check was TOCTOU. It resolved the hostname, said "private — ok,"
    returned. The subsequent httpx call re-resolved, and under
    split-horizon DNS / resolver drift / cache poison could connect
    to a public IP instead. The air-gap contract requires that the
    socket connect to the SAME addresses that passed validation.

    This helper:
      1. Resolves the URL's hostname.
      2. Verifies every resolved address is private/loopback under
         ITAR mode. Mixed answers (one private + one public) are
         refused outright — the public record is the rebinding
         signal.
         Exception: inside `oidc_request_scope()`, a host that is exactly
         in ODIN_ITAR_ALLOWED_IDP_HOSTS may resolve to public addresses
         only (never mixed), over https/443. The resolution is the same
         single lookup, and the connection is pinned to those addresses.
         Each such request writes one `itar.idp_outbound` audit row.
      3. Pins synchronous socket.getaddrinfo (via the ContextVar pin
         installed by `core/webhook_utils`) to the validated list
         for the duration of the context in the connecting thread.
      4. Outside ITAR mode: this is a pure no-op for zero overhead.

    Synchronous usage (disable proxies while ITAR pinning is active):
        with pin_for_request(url):
            with httpx.Client(trust_env=should_trust_env()) as client:
                resp = client.get(url)

    Async callers must use core.outbound_http.pinned_async_request.
    AsyncClient's asyncio DNS executor does not inherit the ContextVar;
    wrapping an AsyncClient request in this context does not pin its DNS.
    """
    if not is_itar_mode() or not url:
        yield
        return

    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ItarOutboundBlocked(
            f"ODIN_ITAR_MODE=1: URL has no hostname: {url!r}"
        )

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # Only an OIDC-client request on an exactly listed host can use the allowance.
    idp_kind = _oidc_scope.get()
    idp_allowed = idp_kind is not None and host.lower() in allowed_idp_hosts()
    idp_used = False

    # Literal-IP short-circuit.
    try:
        addr = ipaddress.ip_address(host)
        if not _is_local_address(addr):
            raise ItarOutboundBlocked(
                f"ODIN_ITAR_MODE=1: literal IP {host!r} is public"
            )
        validated_ips = [str(addr)]
    except ValueError:
        # Hostname: resolve and validate every returned address.
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ItarOutboundBlocked(
                f"ODIN_ITAR_MODE=1: could not resolve {host!r}: {exc}"
            )
        validated: list[str] = []
        public: list[str] = []
        seen: set[str] = set()
        for _fam, _type, _proto, _canon, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                ip_obj = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if _is_local_address(ip_obj):
                if ip_str not in seen:
                    validated.append(ip_str)
                    seen.add(ip_str)
            else:
                public.append(ip_str)
        if public:
            if idp_allowed and not validated:
                # Public-only answer for an allowed IdP host: pin to it.
                _require_idp_transport(host, parsed.scheme, port)
                validated = list(dict.fromkeys(public))
                idp_used = True
            else:
                # Any other public answer, including a mixed one, is a
                # rebinding signal. Refuse outright.
                raise ItarOutboundBlocked(
                    f"ODIN_ITAR_MODE=1: {host!r} resolves to public address(es) "
                    f"{public}; refusing to connect"
                )
        if not validated:
            raise ItarOutboundBlocked(
                f"ODIN_ITAR_MODE=1: {host!r} returned no addresses"
            )
        validated_ips = validated

    if idp_used:
        _record_idp_outbound(host.lower(), idp_kind)

    # Pin for the duration of the context. Reuses the ContextVar
    # pin + socket.getaddrinfo override installed by webhook_utils.
    from core.webhook_utils import _pin_dns
    with _pin_dns(host, port, validated_ips):
        yield


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
