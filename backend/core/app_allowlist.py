"""Fail-closed IP allowlist admission, shared by application middleware."""
import logging
log = logging.getLogger("odin.api")

def _ip_allowlist_denial(session_factory, client_ip):
    """None when the IP allowlist admits client_ip; 403 when it refuses; 503 when the check failed.

    Loopback is always admitted for internal callers; trusted proxies restore
    the remote client before this check. The check fails closed.
    """
    try:
        # Cached per worker for a short TTL (core.ip_allowlist_cache); a failed read denies.
        from core.ip_allowlist_cache import policy
        config = policy(session_factory)
        if not config:
            return None
        from core.remote_access import UNKNOWN_PROXY_CLIENT
        if config.get("enabled") and client_ip == UNKNOWN_PROXY_CLIENT:
            return 403
        import ipaddress
        if not (config and config.get("enabled") and config.get("cidrs")):
            return None
        from core.net_identity import is_loopback
        client_ip = client_ip or "127.0.0.1"
        if is_loopback(client_ip):
            return None
        address = ipaddress.ip_address(client_ip)
        if any(address in ipaddress.ip_network(c, strict=False) for c in config["cidrs"]):
            return None
        return 403
    except Exception as e:
        log.error("IP allowlist check failed — denying request (fail-closed): %s", e)
        return 503

