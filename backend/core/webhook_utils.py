"""
O.D.I.N. — Webhook URL validation helper.

SSRF-safe webhook validation for notifications and organizations.
Configuration rejects literal private addresses and non-HTTP(S) schemes.
Dispatch validates every DNS answer and pins the connection with safe_post.

safe_post() pins the synchronous socket lookup to the validated IPs,
retaining the original hostname for TLS SNI and certificate verification.
"""

import contextlib
import contextvars
import ipaddress
import os
import socket
import ssl
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

    from core.itar import is_itar_mode
    if is_itar_mode():
        # The only webhook ITAR mode can send goes to the local network, so a literal
        # local-network address is accepted here and a literal public one is refused.
        try:
            literal = str(ipaddress.ip_address(host))
        except ValueError:
            return  # a name: checked, and pinned, when the webhook is sent
        if _itar_webhook_refusal(literal):
            raise HTTPException(
                status_code=400,
                detail="ITAR mode is on: a webhook address must be on the local network")
        return

    if _is_disallowed_address(host):
        raise HTTPException(status_code=400, detail="Webhook URL targets a blocked host")


def _itar_webhook_refusal(ip_str: str):
    """Why ITAR mode refuses a webhook to this address, or None when it is allowed.

    Under ITAR mode the only webhook ODIN may send goes to the local network, so the
    public-only SSRF rule cannot apply as written. What is kept of it: loopback,
    link-local (cloud metadata), unspecified and multicast addresses, ODIN's own
    service ports, and the port of its configured go2rtc API and PostgreSQL server on
    any host (see _is_odin_service), stay refused. What is left is RFC 1918 and IPv6 unique-local space.
    """
    from core.itar import is_private_destination
    if not is_private_destination(ip_str.split("%", 1)[0]):
        return "ODIN_ITAR_MODE=1 refused outbound request: the address is outside the local network"
    if is_blocked_device_address(ip_str):
        return "the address is loopback, link-local or otherwise reserved"
    return None


def _plain_address(value):
    """An ip_address with an IPv4 address carried inside IPv6 (mapped, compatible, NAT64)
    reduced to that IPv4 address, so both spellings of one host compare equal."""
    addr = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) \
        else ipaddress.ip_address(str(value).split("%", 1)[0])
    return _embedded_ipv4(addr) or addr


def _is_odin_service(ip_str: str, port: int) -> bool:
    """ITAR mode permits a webhook to the local network, which is where ODIN's own services
    are. Refused: the port of the configured PostgreSQL server or go2rtc API on ANY host
    (configured_service_ports: no lookup, so nothing can disagree with the pinned answer),
    and ODIN's fixed service ports on its own addresses, compared in one spelling."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return True
    if port in configured_service_ports():
        return True
    if port not in own_service_ports():
        return False
    try:
        address = _plain_address(ip_str)
    except ValueError:
        return True
    return address in {_plain_address(own) for own in own_interface_addresses()}


def _resolve_and_pin(url: str, local_only: bool = False) -> tuple[list[str], int, str, str]:
    """Resolve the URL's hostname, validate every returned address, and
    return the tuple needed to pin the upcoming connection:
        (validated_ips, port, original_hostname, scheme)

    Return all validated IPs in resolver order for dual-stack fallback.

    The name is looked up ONCE and one rule is applied to that one answer. By default
    every address must be public (SSRF). With `local_only` (safe_post and trusted_post
    pass it under ITAR mode) every address must be on the local network instead (see
    _itar_webhook_refusal). The caller pins the connection to the returned addresses, so
    no later answer can replace them.
    """
    itar = local_only

    def refusal(ip_str):
        if itar:
            return _itar_webhook_refusal(ip_str)
        return "private/reserved" if _is_disallowed_address(ip_str) else None
    if not url:
        raise WebhookSSRFError("Empty webhook URL")
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        raise WebhookSSRFError(f"Webhook URL has no hostname: {url!r}")
    if parsed.scheme not in ("http", "https"):
        raise WebhookSSRFError(f"Unsupported scheme: {parsed.scheme!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # Literal IP path
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None  # hostname
    if addr is not None:
        ip_str = str(addr)
        if itar and (_itar_webhook_refusal(ip_str) or _is_odin_service(ip_str, port)):
            reason = _itar_webhook_refusal(ip_str) or "it is one of ODIN's own services"
            raise WebhookSSRFError(f"Webhook URL {host} refused: {reason}")
        if not itar and _is_disallowed_address(ip_str):
            raise WebhookSSRFError(
                f"Webhook URL targets a private/reserved address: {host}"
            )
        return [ip_str], port, host, parsed.scheme

    # Hostname path: resolve, reject if ANY resolved address is private.
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise WebhookSSRFError(
            f"Webhook hostname {host!r} could not be resolved: {e}"
        )
    validated: list[str] = []
    seen: set[str] = set()
    disallowed: list[str] = []
    for _fam, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        if refusal(ip_str) or (itar and _is_odin_service(ip_str, port)):
            disallowed.append(ip_str)
        elif ip_str not in seen:
            validated.append(ip_str)
            seen.add(ip_str)
    if disallowed and itar:
        # Same all-or-nothing rule: one address outside the local network (or one of the
        # reserved kinds) refuses the whole answer.
        raise WebhookSSRFError(
            f"ODIN_ITAR_MODE=1 refused outbound request: webhook hostname {host!r} resolves "
            f"to address(es) {disallowed} that are outside the local network or reserved"
        )
    if disallowed:
        # All-or-nothing: any private resolution is a refusal. Don't fall
        # back to "use the public ones and ignore the private ones" —
        # the presence of a private record is itself the rebinding signal.
        raise WebhookSSRFError(
            f"Webhook hostname {host!r} resolves to private/reserved "
            f"address(es) {disallowed}; refusing to dispatch"
        )
    if not validated:
        raise WebhookSSRFError(f"Webhook hostname {host!r} returned no addresses")
    return validated, port, host, parsed.scheme


# ContextVar replaces threading.local() to isolate concurrent tasks and threads.
# asyncio.to_thread copies context, but AsyncClient's asyncio DNS executor does
# NOT. Async HTTP callers must use core.outbound_http.pinned_async_request,
# which validates, pins and connects synchronously in one worker thread.
_dns_pin_state: contextvars.ContextVar = contextvars.ContextVar("odin_dns_pin", default=None)
_real_getaddrinfo = socket.getaddrinfo


def normalize_hostname(host) -> str:
    """Canonical form for pin comparison and storage.

    Lower-case, trailing dots stripped, IDNA (punycode) for non-ASCII names,
    brackets removed and IP literals in their canonical text form.
    """
    if isinstance(host, bytes):
        host = host.decode("ascii", "strict")
    value = str(host or "").strip().strip("[]").rstrip(".").lower()
    try:
        return str(ipaddress.ip_address(value.split("%", 1)[0]))
    except ValueError:
        pass
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


class PinnedLookupRefused(socket.gaierror):
    """A name lookup outside the active DNS pin (fail closed)."""


def _pinned_getaddrinfo(host, port, *args, **kwargs):
    pin = _dns_pin_state.get()
    if not pin:
        return _real_getaddrinfo(host, port, *args, **kwargs)
    try:
        name = normalize_hostname(host)
    except UnicodeError:
        name = None
    port_ok = port is None or port == pin["port"] or str(port) == str(pin["port"])
    if not port_ok or (name != pin["hostname"] and name not in pin["ips"]):
        # Fail closed: while a pin is active nothing reaches the real
        # resolver, so a case/IDNA/trailing-dot variant of the pinned name
        # or a redirect to another name cannot be re-resolved.
        raise PinnedLookupRefused(socket.EAI_NONAME, "lookup outside the pinned destination")
    # Codex pass 3: return ALL pinned IPs in priority order so httpx
    # can fall back across the list (dual-stack AAAA→A retry remains
    # functional on IPv4-only runners).
    results = []
    for ip in pin["ips"]:
        if ":" in ip:
            results.append((
                socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                "", (ip, pin["port"], 0, 0),
            ))
        else:
            results.append((
                socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                "", (ip, pin["port"]),
            ))
    return results


# Install the override exactly once. Multiple imports of this module are
# benign because we only swap when the bound name is still the original.
if socket.getaddrinfo is _real_getaddrinfo:
    socket.getaddrinfo = _pinned_getaddrinfo


@contextlib.contextmanager
def _pin_dns(hostname: str, port: int, ips: list[str]):
    """Pin DNS for `hostname:port` to the validated `ips` list for
    the duration of the context in the connecting thread. ContextVar isolates
    tasks but does not propagate into AsyncClient's asyncio DNS executor.
    Use core.outbound_http.pinned_async_request for async HTTP.
    """
    token = _dns_pin_state.set({"hostname": normalize_hostname(hostname), "port": port,
                                "ips": [normalize_hostname(ip) for ip in ips]})
    try:
        yield
    finally:
        _dns_pin_state.reset(token)


def safe_post(url: str, **kwargs):
    """SSRF-safe POST for USER-SUPPLIED webhook URLs (Discord, Slack,
    ntfy, generic, org webhooks).

    Resolves the URL's hostname, validates ALL returned addresses, then
    pins the TCP connect to one of those validated IPs. TLS SNI +
    certificate verification still target the original hostname so cert
    validation behaves normally.

    trust_env=False prevents a proxy from doing a second, unpinned lookup.
    Context-local pins let concurrent dispatches proceed independently.

    When `ODIN_ITAR_MODE=1` the rule applied to that same single answer is the
    opposite one: every address must be on the local network (RFC 1918 or IPv6
    unique-local; not loopback, link-local or one of ODIN's own service ports).
    Internal webhooks to logging / ticketing systems are the only kind allowed.

    For HARDCODED third-party APIs (Telegram bot API, Pushover, WhatsApp
    Graph API) use trusted_post() instead — those targets are not user-
    controlled, don't need SSRF defense, and may legitimately need to
    traverse an egress proxy.

    Raises WebhookSSRFError on a disallowed target. Re-raises any
    httpx.HTTPError unchanged.
    """
    import httpx as _httpx
    from core.itar import is_itar_mode

    # One lookup. _resolve_and_pin applies the ITAR rule or the SSRF rule to that one
    # answer; a separate ITAR lookup before it let a second, different answer be pinned.
    validated_ips, port, hostname, _scheme = _resolve_and_pin(url, local_only=is_itar_mode())
    timeout = kwargs.pop("timeout", 10)
    with _pin_dns(hostname, port, validated_ips):
        with _httpx.Client(trust_env=False, timeout=timeout) as client:
            return client.post(url, **kwargs)


# Allowlist of hardcoded third-party API hostnames trusted_post() may
# target. If a future call site needs a new vendor, add it here in the
# same change that introduces the call. The runtime check below makes a
# misuse fail loud rather than silently accepting a user-supplied URL.
_TRUSTED_API_HOSTS = frozenset({
    "api.telegram.org",
    "api.pushover.net",
    "graph.facebook.com",
})


def trusted_post(url: str, **kwargs):
    """POST to a HARDCODED third-party API endpoint (not user-supplied).

    Use ONLY for endpoints that are baked into our codebase as string
    literals: Telegram bot API, Pushover, WhatsApp Graph API. The host
    is not attacker-controlled, so SSRF defense is unnecessary, and we
    must respect HTTP_PROXY/HTTPS_PROXY for deployments whose only
    egress route is through a corporate proxy.

    NEVER pass a user-supplied URL here. The hostname must appear in
    _TRUSTED_API_HOSTS — anything else raises WebhookSSRFError so a
    refactor that accidentally pipes user data through this path fails
    immediately rather than silently bypassing the SSRF gate.

    ITAR refuses these public APIs. Use safe_post with an internal relay
    for ITAR notifications.
    """
    import httpx as _httpx
    from core.itar import is_itar_mode

    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in _TRUSTED_API_HOSTS:
        raise WebhookSSRFError(
            f"trusted_post() refused: hostname {host!r} is not in the "
            f"hardcoded allowlist. If this is a user-supplied URL, use "
            f"safe_post() instead. Allowlist: {sorted(_TRUSTED_API_HOSTS)}"
        )
    if parsed.scheme != "https":
        raise WebhookSSRFError(
            f"trusted_post() refused: scheme {parsed.scheme!r} is not https. "
            "Hardcoded API endpoints must always use TLS."
        )

    timeout = kwargs.pop("timeout", 10)
    if is_itar_mode():
        # These hosts are public, so this is refused unless the name answers with local
        # addresses only (an internal relay). One lookup, checked, and the connection is
        # pinned to it with no proxy: a later public answer is never used.
        validated_ips, port, hostname, _scheme = _resolve_and_pin(url, local_only=True)
        with _pin_dns(hostname, port, validated_ips):
            with _httpx.Client(trust_env=False, timeout=timeout) as client:
                return client.post(url, **kwargs)
    # No DNS pin, no trust_env override — these are trusted targets whose
    # hostnames we own at compile time. Egress proxy is honored.
    return _httpx.post(url, timeout=timeout, **kwargs)


def resolve_and_check_webhook_url(url: str) -> str:
    """Resolve a webhook URL's hostname and reject if any address is private.

    Unlike configuration-time literal checks, this checks DNS answers too.
    Do not cache the result. This validation alone cannot prevent rebinding
    between check and connect; use safe_post for pinned webhook dispatch.

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

    from core.itar import is_itar_mode
    if is_itar_mode():
        # No lookup here under ITAR mode: safe_post does the one lookup, applies the
        # local-network rule to it and pins the connection. The public-only rule below
        # would refuse every webhook ITAR mode permits.
        return url

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


# ====================================================================
# LAN device destinations (printers, smart plugs, cameras)
# ====================================================================
#
# LAN devices allow RFC-1918/ULA but reject special/own endpoints, including
# embedded IPv4. Every resolved address must pass. pinned_device_destination
# binds the synchronous connection to these validated addresses.

class DeviceDestinationBlocked(WebhookSSRFError):
    """A device host is malformed, unresolvable or a blocked address."""


_METADATA_ADDRESSES = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata
})
_IPV4_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
_IPV6_COMPAT = ipaddress.ip_network("::/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(addr):
    if addr.version != 6:
        return None
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr in _NAT64 or (addr in _IPV6_COMPAT and int(addr) > 1):
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return None


def is_blocked_device_address(value) -> bool:
    """True when `value` (an IP string or ip_address) must never be a device target."""
    try:
        addr = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) \
            else ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return True
    candidates = [addr]
    embedded = _embedded_ipv4(addr)
    if embedded is not None:
        candidates.append(embedded)
    for a in candidates:
        if (a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast
                or a in _METADATA_ADDRESSES):
            return True
        if a.version == 4 and (a in _IPV4_THIS_NETWORK or a.is_reserved):
            return True
    return False


# ODIN's own service ports: go2rtc RTSP (8554), WebRTC (8555), go2rtc API (1984) and the
# ODIN API (8000, or PORT). A device URL naming one of ODIN's own addresses on one of
# these ports would make ODIN re-serve its own streams (e.g. another org's camera via
# rtsp://<container IP>:8554/printer_<id>), so it is refused.
_OWN_SERVICE_PORTS = (8554, 8555, 1984, 8000)


def own_service_ports() -> frozenset:
    ports = set(_OWN_SERVICE_PORTS)
    for name in ("PORT", "ODIN_PORT", "ODIN_GO2RTC_PORT", "ODIN_WEBRTC_PORT"):
        try:
            value = int(os.environ.get(name, "") or 0)
        except ValueError:
            continue
        if 0 < value < 65536:
            ports.add(value)
    return frozenset(ports)


def _proc_ipv4_local():
    found = set()
    try:
        with open("/proc/net/fib_trie") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return found
    for previous, line in zip(lines, lines[1:]):
        if "/32 host LOCAL" in line:
            candidate = previous.strip().split()[-1]
            found.add(candidate)
    return found


def _proc_ipv6_local():
    found = set()
    try:
        with open("/proc/net/if_inet6") as handle:
            for line in handle:
                raw = line.split()[0] if line.split() else ""
                if len(raw) == 32:
                    found.add(str(ipaddress.IPv6Address(int(raw, 16))))
    except (OSError, ValueError):
        pass
    return found


def _proc_default_gateways():
    """The container's default gateway: in Docker bridge mode the host, whose published
    ports (8000, 8555) reach ODIN."""
    found = set()
    try:
        with open("/proc/net/route") as handle:
            for line in handle.read().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    found.add(str(ipaddress.IPv4Address(int(fields[2], 16).to_bytes(4, "little"))))
    except (OSError, ValueError):
        pass
    return found


def own_interface_addresses() -> frozenset:
    """Every address that reaches this ODIN instance, enumerated now (not cached):
    the container's/host's interfaces, its hostname's addresses, the default gateway
    (the Docker host), the configured host IP (ODIN_HOST_IP / system_config host_ip) and
    a separate go2rtc service's addresses (ODIN_GO2RTC_API_URL)."""
    addresses = _proc_ipv4_local() | _proc_ipv6_local() | _proc_default_gateways()
    try:
        for info in _real_getaddrinfo(socket.gethostname(), None):
            addresses.add(info[4][0])
    except (socket.gaierror, OSError, UnicodeError):
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
            addresses.add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    configured = os.environ.get("ODIN_HOST_IP")
    if configured:
        addresses.add(configured.strip())
    # A separate go2rtc service (enterprise compose) is part of ODIN too: its API and
    # RTSP ports re-serve every printer's stream.
    go2rtc_url = (os.environ.get("ODIN_GO2RTC_API_URL") or "").strip()
    if go2rtc_url:
        from urllib.parse import urlsplit
        go2rtc_host = urlsplit(go2rtc_url).hostname
        if go2rtc_host:
            try:
                for info in _real_getaddrinfo(go2rtc_host, None):
                    addresses.add(info[4][0])
            except (socket.gaierror, OSError, UnicodeError):
                pass
    try:
        from core.db_utils import get_db
        with get_db() as conn:
            row = conn.execute("SELECT value FROM system_config WHERE key = 'host_ip'").fetchone()
        if row and row[0]:
            value = row[0]
            if isinstance(value, str):
                value = value.strip().strip('"')
            addresses.add(str(value))
    except Exception:
        pass
    normalized = set()
    for value in addresses:
        try:
            normalized.add(ipaddress.ip_address(str(value).split("%", 1)[0]))
        except ValueError:
            continue
    return frozenset(normalized)


def is_own_service_endpoint(ip, port) -> bool:
    """True when (ip, port) is one of ODIN's own service ports on one of its own addresses."""
    if not port or int(port) not in own_service_ports():
        return False
    try:
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return True
    own = own_interface_addresses()
    embedded = _embedded_ipv4(addr)
    return addr in own or (embedded is not None and embedded in own)


def configured_service_ports() -> frozenset:
    """The port numbers of the services this installation is configured to use, with no
    lookup: the PostgreSQL server (the port in the database URL and the maintenance URL, or
    5432) and the go2rtc API (the port in ODIN_GO2RTC_API_URL, or the scheme's own port, 80
    or 443, when it names none: that is where the request goes).

    The database URLs are read from core.config.settings, the object the engine is built
    from, never from os.environ: the settings also come from a .env file and from variable
    names in any letter case, which a look at os.environ["DATABASE_URL"] would miss.

    Used for webhooks under ITAR mode, where the test is the port alone, on any host. A host
    comparison would need a name lookup that can disagree with the one the webhook is
    pinned to; a port needs none.
    """
    from core import config
    ports = set()
    go2rtc_url = (os.environ.get("ODIN_GO2RTC_API_URL") or "").strip()
    if go2rtc_url:
        try:
            parsed = urllib.parse.urlsplit(go2rtc_url)
            ports.add(parsed.port or (443 if parsed.scheme.lower() == "https" else 80))
        except ValueError:
            pass
    for value in (config.settings.database_url, config.settings.database_maintenance_url):
        value = str(value or "").strip()
        if not value.lower().startswith(("postgresql", "postgres")):
            continue
        try:
            ports.add(urllib.parse.urlsplit(value).port or 5432)
        except ValueError:
            ports.add(5432)
    return frozenset(ports)


def split_device_host(value: str):
    """Parse `host`, `host:port`, `[v6]`, `[v6]:port` or a bare IPv6 literal.

    Returns (hostname, port_or_None). Raises DeviceDestinationBlocked when
    the value is not a plain authority (userinfo, path, query, bad port).
    """
    if not isinstance(value, str) or not value.strip():
        raise DeviceDestinationBlocked("Empty host")
    raw = value.strip()
    if any(ch in raw for ch in "/?#@\\ \t"):
        raise DeviceDestinationBlocked("Host must be host or host:port")
    if raw.count(":") > 1 and not raw.startswith("["):
        try:
            return str(ipaddress.IPv6Address(raw.split("%", 1)[0])), None
        except ValueError:
            raise DeviceDestinationBlocked("Invalid IPv6 host; use [address]:port")
    try:
        parsed = urllib.parse.urlsplit("//" + raw)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise DeviceDestinationBlocked("Invalid host or port")
    if not host:
        raise DeviceDestinationBlocked("Invalid host")
    return host.rstrip("."), port


def normalize_device_host(value: str) -> str:
    """Stored form of a device host[:port]: normalized name, brackets for IPv6."""
    hostname, port = split_device_host(value)
    name = normalize_hostname(hostname)
    shown = f"[{name}]" if ":" in name else name
    return f"{shown}:{port}" if port else shown


def normalize_device_url(url: str) -> str:
    """http(s)://host[:port] with the host normalized (scheme lower-cased)."""
    parsed = urllib.parse.urlsplit(url.strip())
    name = normalize_hostname(parsed.hostname or "")
    shown = f"[{name}]" if ":" in name else name
    netloc = f"{shown}:{parsed.port}" if parsed.port else shown
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, parsed.fragment))


ITAR_DEVICE_REFUSAL = "ITAR mode blocks printer and device addresses outside the local network."


def _refuse_public_device_addresses_under_itar(ips) -> None:
    """ITAR mode: every address a device host resolved to must be on the local network.

    The rule is core.itar.is_private_destination (the one the Moonraker, PrusaLink and
    go2rtc guards use). One address outside it refuses the whole answer, so a name that
    resolves to a local and a public address is refused. No-op with ITAR mode off.
    """
    from core.itar import is_itar_mode, is_private_destination
    if not is_itar_mode():
        return
    for ip in ips:
        if not is_private_destination(str(ip).split("%", 1)[0]):
            raise DeviceDestinationBlocked(ITAR_DEVICE_REFUSAL)


def device_destination_ips(host: str, port=None) -> list:
    """Resolve a device host and return its validated addresses.

    `host` may carry a port (see split_device_host). Raises
    DeviceDestinationBlocked if it cannot be resolved or ANY resolved
    address is blocked. Under ITAR mode every address must also be on the
    local network. Every device client gets its address here (directly, or
    through pinned_device_destination, device_request, device_urlopen and
    device_connect_ip), so this is the one place that rule is enforced.
    """
    hostname, parsed_port = split_device_host(host)
    try:
        hostname = normalize_hostname(hostname)
    except UnicodeError as exc:
        raise DeviceDestinationBlocked("Invalid host name") from exc
    port = port or parsed_port or 0
    try:
        infos = _real_getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise DeviceDestinationBlocked(f"Host {hostname!r} could not be resolved") from exc
    ips = []
    for _fam, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        if is_blocked_device_address(ip):
            raise DeviceDestinationBlocked(f"Host {hostname!r} resolves to a blocked address")
        if is_own_service_endpoint(ip, port):
            raise DeviceDestinationBlocked(f"Host {hostname!r} port {port} is one of ODIN's own services")
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise DeviceDestinationBlocked(f"Host {hostname!r} returned no addresses")
    _refuse_public_device_addresses_under_itar(ips)
    return ips


@contextlib.contextmanager
def pinned_device_destination(url: str):
    """Validate a device URL and pin its connection to the validated addresses.

    Every socket.getaddrinfo for the URL's host:port inside the block
    (urllib, requests, httpx) returns only the addresses checked here.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise DeviceDestinationBlocked("Invalid device URL") from exc
    if parsed.scheme.lower() not in ("http", "https") or not hostname:
        raise DeviceDestinationBlocked("Device URL must be http(s)://host")
    port = port or (443 if parsed.scheme.lower() == "https" else 80)
    ips = device_destination_ips(hostname if ":" not in hostname else f"[{hostname}]", port)
    with _pin_dns(hostname, port, ips):
        yield ips


# --------------------------------------------------------------------
# Device HTTP clients: pinned, no redirects, no environment proxies
import urllib.request as _urllib_request


class _NoRedirect(_urllib_request.HTTPRedirectHandler):
    """A 3xx surfaces as HTTPError; a device must never send ODIN elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def device_urlopen(request, timeout=None):
    """urllib open for a device URL: policy-validated, pinned, no redirect, no proxy."""
    url = request.full_url if isinstance(request, _urllib_request.Request) else request
    opener = _urllib_request.build_opener(_urllib_request.ProxyHandler({}), _NoRedirect())
    with pinned_device_destination(url):
        return opener.open(request, timeout=timeout)


def device_request(method: str, url: str, **kwargs):
    """requests call for a device URL: policy-validated, pinned, no redirect, no proxy.

    A fresh Session ignores HTTP(S)_PROXY/ALL_PROXY/netrc (trust_env=False,
    proxies={}); redirects are never followed. Raises DeviceDestinationBlocked
    for a blocked destination.
    """
    import requests
    kwargs["allow_redirects"] = False
    kwargs.pop("proxies", None)
    with requests.Session() as session:
        session.trust_env = False
        session.proxies = {}
        session.max_redirects = 0
        with pinned_device_destination(url):
            return session.request(method, url, **kwargs)


def device_connect_ip(host: str, port=None) -> str:
    """Validated IP literal to connect to for a non-HTTP device channel.

    MQTT, FTPS, SDCP websocket and UDP clients connect to this literal (no
    second DNS lookup, so no rebinding window). TLS callers keep the original
    name for server_hostname where their certificate policy uses it.
    Raises DeviceDestinationBlocked.
    """
    value = str(host or "").strip()
    if value.count(":") > 1 and not value.startswith("["):
        value = f"[{value}]"
    return device_destination_ips(value, port)[0]

