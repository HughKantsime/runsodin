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

Codex pass 2 (2026-04-13):
  * safe_post() now PINS the resolved IP to the connection. The previous
    implementation validated DNS, then handed the original hostname back
    to httpx, which performed a fresh resolution — leaving a
    DNS-rebinding window where the second lookup could return a private
    IP. The new implementation builds a custom httpx transport whose
    socket connect is locked to one of the validated IPs, with TLS SNI
    and certificate verification still pointed at the original hostname.
"""

import contextlib
import ipaddress
import socket
import ssl
import threading
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


def _resolve_and_pin(url: str) -> tuple[list[str], int, str, str]:
    """Resolve the URL's hostname, validate every returned address, and
    return the tuple needed to pin the upcoming connection:
        (validated_ips, port, original_hostname, scheme)

    Codex pass 3 (2026-04-13): returns the FULL list of validated IPs in
    `getaddrinfo` priority order, not just the first one. Pinning a
    single IP broke dual-stack hosts (AAAA returned first, IPv4-only
    runner could never connect). The pin override hands all of them
    back to httpx, which can fall back across the list as before.
    """
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
        ip_str = str(addr)
        if _is_disallowed_address(ip_str):
            raise WebhookSSRFError(
                f"Webhook URL targets a private/reserved address: {host}"
            )
        return [ip_str], port, host, parsed.scheme
    except ValueError:
        pass  # hostname

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
        if _is_disallowed_address(ip_str):
            disallowed.append(ip_str)
        elif ip_str not in seen:
            validated.append(ip_str)
            seen.add(ip_str)
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


# Codex pass 3 (2026-04-13): replaced the process-wide lock with a
# thread-local pin state. The previous lock-around-the-whole-POST
# serialised every concurrent webhook dispatch behind one slow endpoint.
# Thread-local lets each dispatcher thread carry its own pinned IP
# without contention; the resolver override below routes the lookup based
# on which thread is asking.
#
# The override is installed once at module load (it's a no-op when no
# thread has a pin set), so we never mutate socket.getaddrinfo at runtime.
_dns_pin_state = threading.local()
_real_getaddrinfo = socket.getaddrinfo


def _pinned_getaddrinfo(host, port, *args, **kwargs):
    pin = getattr(_dns_pin_state, "pin", None)
    if pin and host == pin["hostname"] and (
        port == pin["port"] or port is None or str(port) == str(pin["port"])
    ):
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
    return _real_getaddrinfo(host, port, *args, **kwargs)


# Install the override exactly once. Multiple imports of this module are
# benign because we only swap when the bound name is still the original.
if socket.getaddrinfo is _real_getaddrinfo:
    socket.getaddrinfo = _pinned_getaddrinfo


@contextlib.contextmanager
def _pin_dns(hostname: str, port: int, ips: list[str]):
    """Pin DNS for `hostname:port` to the validated `ips` list on the
    current thread only. httpx may try them in order until one connects.

    Thread-local: other threads dispatching to other webhooks see no
    interference. Defeats the DNS-rebinding TOCTOU window between the
    validation call and httpx's socket connect.
    """
    previous = getattr(_dns_pin_state, "pin", None)
    _dns_pin_state.pin = {"hostname": hostname, "port": port, "ips": list(ips)}
    try:
        yield
    finally:
        _dns_pin_state.pin = previous


def safe_post(url: str, **kwargs):
    """SSRF-safe POST for USER-SUPPLIED webhook URLs (Discord, Slack,
    ntfy, generic, org webhooks).

    Resolves the URL's hostname, validates ALL returned addresses, then
    pins the TCP connect to one of those validated IPs. TLS SNI +
    certificate verification still target the original hostname so cert
    validation behaves normally.

    Codex pass 2 (2026-04-13) closed the DNS-rebinding TOCTOU.
    Codex pass 3 (2026-04-13) closed:
      * Outbound proxy bypass: `trust_env=False` makes httpx ignore
        HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY env vars. With those
        honored, the connection would go to the proxy (which then does
        its own attacker-controlled DNS lookup) and the pinning is
        useless. Webhook dispatch must connect direct.
      * Throughput collapse: replaced the global lock with a thread-local
        pin so concurrent webhook dispatches no longer serialise behind
        one slow endpoint.

    v1.8.9: when `ODIN_ITAR_MODE=1`, refuses public destinations at
    call time (before DNS pinning). Private destinations (internal
    webhooks to logging / ticketing systems) are still allowed.

    For HARDCODED third-party APIs (Telegram bot API, Pushover, WhatsApp
    Graph API) use trusted_post() instead — those targets are not user-
    controlled, don't need SSRF defense, and may legitimately need to
    traverse an egress proxy.

    Raises WebhookSSRFError on a disallowed target. Re-raises any
    httpx.HTTPError unchanged.
    """
    import httpx as _httpx
    from core.itar import enforce_request_destination, ItarOutboundBlocked

    try:
        enforce_request_destination(url)
    except ItarOutboundBlocked as exc:
        raise WebhookSSRFError(str(exc)) from exc

    validated_ips, port, hostname, _scheme = _resolve_and_pin(url)
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

    Codex pass 3 (2026-04-13): introduced specifically because
    safe_post()'s `trust_env=False` broke proxy-only egress for the
    hardcoded API call sites in channels.py.

    v1.8.9: when `ODIN_ITAR_MODE=1`, ALL trusted-API calls are refused
    — the allowlisted hosts (api.telegram.org etc.) are all public
    and violate the air-gap posture. Operators who need per-site
    notification in ITAR deployments must set up an internal relay
    and use user-configured webhooks (which safe_post validates for
    privateness).
    """
    import httpx as _httpx
    from core.itar import enforce_request_destination, ItarOutboundBlocked

    try:
        enforce_request_destination(url)
    except ItarOutboundBlocked as exc:
        raise WebhookSSRFError(str(exc)) from exc

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
    # No DNS pin, no trust_env override — these are trusted targets whose
    # hostnames we own at compile time. Egress proxy is honored.
    return _httpx.post(url, timeout=timeout, **kwargs)


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
