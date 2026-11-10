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

4. One narrow, explicit exception: `ODIN_ITAR_ALLOWED_IDP_HOSTS` lists exact
   hostnames of an OIDC identity provider (for example Microsoft Entra ID
   or GCC High). Only the OIDC client (`purpose="oidc"`) may reach a listed
   host over a public address, https and port 443 only. Every other caller
   is unchanged.

The two checks can coexist because they're layered: SSRF protects
against attackers supplying a private URL; ITAR protects against
operators (or admins) accidentally configuring a public URL when they
shouldn't be able to reach public destinations at all.

Env-var toggle (not a DB setting) because ITAR posture is a
container-level decision that cannot be flipped by a compromised
admin account. Operators set it in their compose / systemd unit and
a misconfiguration fails loud at boot. The identity-provider allowance is
env-only for the same reason.
"""

from __future__ import annotations

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

# Identity-provider allowance (env only).
IDP_ALLOWANCE_ENV = "ODIN_ITAR_ALLOWED_IDP_HOSTS"
IDP_ALLOWANCE_MAX_HOSTS = 8
# The only purpose that may use the allowance. Passed explicitly by the OIDC
# client; never inferred from the URL or the caller's module.
PURPOSE_OIDC = "oidc"
_HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class IdpDiscoveryUrl(str):
    """An OIDC discovery URL collected for the boot audit.

    The boot audit treats these differently from every other URL: a public
    host is acceptable when it is listed in ODIN_ITAR_ALLOWED_IDP_HOSTS.
    """


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


def _idp_host_error(entry: str) -> Optional[str]:
    """Why `entry` is not an acceptable allowance entry, or None."""
    if not entry:
        return "empty entry"
    if entry != entry.lower():
        return "must be lowercase"
    if "*" in entry:
        return "wildcards are not allowed"
    try:
        ipaddress.ip_address(entry.strip("[]"))
        return "IP addresses are not allowed"
    except ValueError:
        pass
    if any(ch in entry for ch in "/:@?#[]\\ \t"):
        return "must be a bare hostname (no scheme, port, path or credentials)"
    if len(entry) > 253:
        return "hostname is too long"
    labels = entry.split(".")
    if not all(_HOST_LABEL.match(label) for label in labels):
        return "not a valid hostname (letters, digits and hyphens; no trailing dot)"
    if labels[-1].isdigit():
        return "looks like an IP address"
    return None


def parse_idp_allowance(raw: Optional[str]) -> tuple[list[str], list[str]]:
    """Parse ODIN_ITAR_ALLOWED_IDP_HOSTS into (hosts, errors).

    Comma-separated exact lowercase hostnames; no wildcards, no IP literals,
    at most IDP_ALLOWANCE_MAX_HOSTS distinct entries. Whitespace around an
    entry is ignored. Unset or blank means "no allowance" (no errors).
    """
    if raw is None or not raw.strip():
        return [], []
    hosts: list[str] = []
    errors: list[str] = []
    for part in raw.split(","):
        entry = part.strip()
        problem = _idp_host_error(entry)
        if problem:
            errors.append(f"{entry!r}: {problem}")
        elif entry not in hosts:
            hosts.append(entry)
    if len(hosts) > IDP_ALLOWANCE_MAX_HOSTS:
        errors.append(
            f"{len(hosts)} hosts listed; the maximum is {IDP_ALLOWANCE_MAX_HOSTS}"
        )
    return hosts, errors


def idp_allowance_errors() -> list[str]:
    """Problems with the current env value (empty list when valid or unset)."""
    return parse_idp_allowance(os.getenv(IDP_ALLOWANCE_ENV))[1]


def idp_allowed_hosts() -> frozenset:
    """The active allowance. Empty unless ITAR mode is on and the env value
    is valid: an invalid value fails closed (boot refuses it separately)."""
    if not is_itar_mode():
        return frozenset()
    hosts, errors = parse_idp_allowance(os.getenv(IDP_ALLOWANCE_ENV))
    if errors:
        return frozenset()
    return frozenset(hosts)


def _idp_public_policy_problem(parsed, port) -> Optional[str]:
    """Rules for a host the operator listed: https, port 443, no credentials."""
    if parsed.scheme != "https":
        return "identity-provider hosts require https"
    if port != 443:
        return "identity-provider hosts require port 443"
    if parsed.username or parsed.password:
        return "credentials in the URL are not allowed"
    return None


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


def check_idp_endpoint(url: Optional[str]) -> tuple[bool, str]:
    """Return (allowed, reason) for an OIDC identity-provider URL.

    Used for the OIDC discovery URL at boot and for each OIDC request and
    discovery-document endpoint at runtime. Allowed iff the host is listed in
    ODIN_ITAR_ALLOWED_IDP_HOSTS (then https, port 443 and no credentials are
    required) or resolves only to private addresses (today's rule, unchanged).
    Blocking DNS: call from a thread in async code.
    """
    if not url or not isinstance(url, str):
        return False, "URL is empty"
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False, "URL is malformed"
    if not host:
        return False, "URL has no host"
    if host in idp_allowed_hosts():
        problem = _idp_public_policy_problem(parsed, port)
        if problem:
            return False, f"{host}: {problem}"
        return True, f"{host} is listed in {IDP_ALLOWANCE_ENV}"
    if is_private_destination(host):
        return True, f"{host} resolves to a private range"
    return False, (
        f"{host} is not listed in {IDP_ALLOWANCE_ENV} and does not resolve "
        "to a private address"
    )


def audit_boot_config(urls: Iterable[str]) -> list[str]:
    """Inspect a list of configured URLs for ITAR compliance.

    Returns a list of violations (strings). Empty list means clean boot.
    Caller is responsible for refusing to start if the list is non-empty.
    `IdpDiscoveryUrl` entries are judged by `check_idp_endpoint`; everything
    else by `check_url_allowed` (the allowance never applies to it).
    """
    violations: list[str] = []
    for url in urls:
        if isinstance(url, IdpDiscoveryUrl):
            ok, reason = check_idp_endpoint(url)
        else:
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

    errors = idp_allowance_errors()
    if errors:
        msg = (
            f"ODIN_ITAR_MODE=1 is set but {IDP_ALLOWANCE_ENV} is invalid:\n"
            + "\n".join(f"  - {error}" for error in errors)
            + "\n\nUse comma-separated exact lowercase hostnames (no wildcards, "
            f"no IP addresses, at most {IDP_ALLOWANCE_MAX_HOSTS}), or unset it."
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
    allowed = sorted(idp_allowed_hosts())
    log.info(
        "ODIN_ITAR_MODE=1 boot audit passed — all configured URLs are private%s.",
        (
            f" or an allowed OIDC identity provider ({IDP_ALLOWANCE_ENV}="
            f"{','.join(allowed)}; https/443, OIDC client only)"
            if allowed else ""
        ),
    )


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
      - `oidc_config.discovery_url` of an enabled config, tagged as an
        `IdpDiscoveryUrl` so the audit can apply the identity-provider
        allowance to it and to nothing else.

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
        # (or an explicitly allowed identity-provider host) or the
        # service should fail at boot rather than at first login.
        try:
            rows = db.execute(
                sa_text(
                    "SELECT discovery_url FROM oidc_config "
                    "WHERE is_enabled IS TRUE AND discovery_url IS NOT NULL"
                )
            ).fetchall()
            for row in rows:
                url = row[0] if row else None
                if url:
                    urls.append(IdpDiscoveryUrl(str(url)))
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


import contextlib


@contextlib.contextmanager
def pin_for_request(url: str, *, purpose: Optional[str] = None):
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
      3. Pins synchronous socket.getaddrinfo (via the ContextVar pin
         installed by `core/webhook_utils`) to the validated list
         for the duration of the context in the connecting thread.
      4. Outside ITAR mode: this is a pure no-op for zero overhead.

    The one exception is `purpose="oidc"` (passed explicitly by the OIDC
    client, never inferred): when the URL's host is exactly listed in
    ODIN_ITAR_ALLOWED_IDP_HOSTS the call must be https on port 443 without
    URL credentials, and the host may resolve to public addresses. The pin
    then covers those public addresses, and a private+public mixed answer
    is still refused. Any other purpose (including None) gets the rules
    above with no exception.

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

    # Exact, case-normalized host match (urlparse lowercases hostname; a
    # trailing dot or a subdomain is a different host). Literal IPs can never
    # be in the allowance.
    allow_public = purpose == PURPOSE_OIDC and host in idp_allowed_hosts()
    if allow_public:
        problem = _idp_public_policy_problem(parsed, port)
        if problem:
            raise ItarOutboundBlocked(
                f"ODIN_ITAR_MODE=1: identity-provider host {host!r} refused: {problem}"
            )

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
            if not allow_public:
                # Any public answer is a rebinding signal. Refuse outright.
                raise ItarOutboundBlocked(
                    f"ODIN_ITAR_MODE=1: {host!r} resolves to public address(es) "
                    f"{public}; refusing to connect"
                )
            if validated:
                # An allowed host gets no leniency on mixed answers.
                raise ItarOutboundBlocked(
                    f"ODIN_ITAR_MODE=1: identity-provider host {host!r} resolves "
                    f"to both private {validated} and public {public} addresses "
                    "(mixed answer); refusing to connect"
                )
            validated = list(dict.fromkeys(public))
        if not validated:
            raise ItarOutboundBlocked(
                f"ODIN_ITAR_MODE=1: {host!r} returned no addresses"
            )
        validated_ips = validated

    # Pin for the duration of the context. Reuses the ContextVar
    # pin + socket.getaddrinfo override installed by webhook_utils.
    from core.webhook_utils import _pin_dns
    with _pin_dns(host, port, validated_ips):
        yield


def enforce_request_destination(url: str) -> None:
    """Raise ItarOutboundBlocked if ITAR is on and the URL is public.

    Called by `safe_post` / `trusted_post` at the top of each request.
    No-op if ITAR mode is off. The identity-provider allowance does not
    apply here.
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
    than a URL. The identity-provider allowance does not apply here.

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
