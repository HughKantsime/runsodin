"""Remote origin configuration and the explicit reverse-proxy trust boundary."""
import asyncio
import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urlsplit

# Fixed public, non-loopback identity for malformed trusted forwarding. Reserved
# for PCP anycast; never contacted. Enabled IP allowlists explicitly reject it.
UNKNOWN_PROXY_CLIENT = "192.0.0.9"
PROXY_DNS_CACHE_SECONDS = 5
PROXY_DNS_TIMEOUT_SECONDS = 2


_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


def _valid_hostname(value):
    """RFC 1123 hostname: 1-253 chars, dot-separated 1-63 char labels, no edge hyphens."""
    name = value[:-1] if value.endswith(".") else value
    if not 1 <= len(name) <= 253:
        return False
    return all(1 <= len(label) <= 63 and set(label) <= _LABEL_CHARS
               and not label.startswith("-") and not label.endswith("-")
               for label in name.split("."))


class TrustedProxyAddresses:
    """Explicit IPs plus short-lived DNS answers from the container resolver.

    Expired answers are never used if resolution fails. Resolve off the event
    loop, coalesce concurrent refreshes, and bound both lookup time and cache age.
    """
    def __init__(self, configured):
        self.addresses = set()
        self.hostnames = set()
        for value in configured.split(","):
            value = value.strip()
            if not value:
                continue
            try:
                self.addresses.add(ipaddress.ip_address(value))
            except ValueError:
                if not _valid_hostname(value):
                    raise ValueError("ODIN_TRUSTED_PROXIES requires IP literals or hostnames")
                self.hostnames.add(value)
        self._resolved = set()
        self._expires = 0
        self._lock = asyncio.Lock()

    async def _resolve(self, hostname):
        try:
            answers = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM),
                timeout=PROXY_DNS_TIMEOUT_SECONDS,
            )
            return {ipaddress.ip_address(answer[4][0]) for answer in answers}
        except (OSError, ValueError, asyncio.TimeoutError):
            return set()

    async def contains(self, peer):
        if peer in self.addresses:
            return True
        if peer is None or not self.hostnames:
            return False
        if time.monotonic() >= self._expires:
            async with self._lock:
                if time.monotonic() >= self._expires:
                    answers = await asyncio.gather(*(self._resolve(h) for h in self.hostnames))
                    self._resolved = set().union(*answers)
                    self._expires = time.monotonic() + PROXY_DNS_CACHE_SECONDS
        return peer in self._resolved


def validate_public_origin(value):
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in value)
                 and re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname))
        port = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("ODIN_PUBLIC_URL must be an HTTPS origin without credentials, path, query or fragment")
    return f"https://{parsed.hostname.lower()}" + (f":{port}" if port and port != 443 else "")


def validate_tailnet_ip(value):
    if not value:
        return ""
    try:
        ip = ipaddress.ip_address(value)
        if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
            return str(ip)
    except ValueError:
        pass
    raise ValueError("ODIN_TAILNET_IP must be the sidecar's Tailscale IPv4 address")


def trusted_hosts(settings):
    hosts = [h.strip() for h in settings.trusted_hosts.split(",") if h.strip()]
    if settings.odin_public_url:
        hosts.append(urlsplit(settings.odin_public_url).hostname)
    return list(dict.fromkeys(hosts)) or ["localhost", "127.0.0.1", "testserver"]


def cors_origins(settings):
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if "*" in origins:
        logging.getLogger("odin.api").warning("CORS wildcard ignored: configure explicit origins for credentials")
        origins = []  # Credentials require explicit origins.
    if settings.odin_public_url:
        origins.append(settings.odin_public_url)
    return list(dict.fromkeys(origins))


def session_cookie_secure(request, settings):
    # Preserve COOKIE_SECURE on LAN; verified HTTPS always upgrades it.
    return request.url.scheme == "https" or settings.cookie_secure


class TrustedProxySchemeMiddleware:
    """Trust configured peer IPs/hostnames, never headers naming a proxy.

    Uvicorn MUST run with --no-proxy-headers so this sees the socket peer.
    Serve preserves Host and appends the real client to X-Forwarded-For.
    Restore that identity before HTTP/WebSocket auth, allowlisting and auditing.
    """
    def __init__(self, app, trusted=""):
        self.app = app
        self.trusted = TrustedProxyAddresses(trusted)
        self._warned_invalid_client = False

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            try:
                peer = ipaddress.ip_address((scope.get("client") or ("", 0))[0])
            except ValueError:
                peer = None
            headers = scope.get("headers", [])
            proto = [v for k, v in headers if k.lower() == b"x-forwarded-proto"]
            forwarded = [v for k, v in headers if k.lower() == b"x-forwarded-for"]
            if (proto or forwarded) and await self.trusted.contains(peer):
                try:
                    # Multiple header fields are one ordered comma-separated list.
                    # Only the last entry is appended by our immediate proxy.
                    value = forwarded[-1].rsplit(b",", 1)[-1].strip().decode("ascii")
                    address = ipaddress.ip_address(value)
                    if getattr(address, "scope_id", None) is not None:
                        raise ValueError("Scoped client address")
                    # A forwarded client never inherits loopback privileges.
                    if (address.is_loopback or address.is_unspecified
                            or getattr(address, "ipv4_mapped", None) is not None):
                        raise ValueError("Disallowed forwarded client address")
                    client = str(address)
                except (IndexError, ValueError, UnicodeError):
                    client = UNKNOWN_PROXY_CLIENT
                    if not self._warned_invalid_client:
                        self._warned_invalid_client = True
                        logging.getLogger("odin.api").warning(
                            "Trusted proxy forwarding has no valid client IP; using denied identity")
                scope = dict(scope, client=(client, 0))
                if proto == [b"https"]:
                    scope["scheme"] = "wss" if scope["type"] == "websocket" else "https"
        await self.app(scope, receive, send)


def add_tailnet_candidate(config):
    from core.config import settings
    if settings.odin_tailnet_ip:
        candidate = f"{settings.odin_tailnet_ip}:8555"
        candidates = config.setdefault("candidates", [])
        if candidate not in candidates:
            candidates.append(candidate)
