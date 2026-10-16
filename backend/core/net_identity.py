"""Shared network-identity helpers so the allowlist route and the perimeter
middleware agree on what "loopback" means."""
import ipaddress


def is_loopback(client_ip) -> bool:
    """True when the caller's effective address is loopback (always admitted)."""
    if not client_ip:
        return False
    try:
        return ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        return client_ip in ("127.0.0.1", "::1", "localhost")
