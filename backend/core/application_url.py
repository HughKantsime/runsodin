"""The application's public URL used in emailed links (password reset).

Admins set it in Settings > Notifications (PUT /api/smtp-config base_url);
links are never built from the request Host header.
"""
import ipaddress
import logging
from urllib.parse import urlsplit

from core.models import SystemConfig

log = logging.getLogger("odin.api")
# LAN-only names: single-label hosts and the reserved local suffixes.
_LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localhost")


def _is_lan_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(_LAN_SUFFIXES) or "." not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def validate_base_url(base):
    """Return the normalized application URL used in emailed links, or raise ValueError.

    Admins set it in Settings > Notifications (PUT /api/smtp-config base_url);
    links are never built from the request Host header.
    """
    if not isinstance(base,str) or not base or any(char.isspace() or ord(char)<32 or ord(char)==127 for char in base):
        raise ValueError('Invalid base URL')
    parsed=urlsplit(base)
    if parsed.scheme.lower() not in {'http','https'} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError('Invalid base URL')
    parsed.port  # Validate a supplied port without emitting the configured value.
    if parsed.scheme.lower() == 'http':
        # Plain http is the documented LAN setup (http://<server-ip>:8000); a
        # public host must use https so reset links are not sent in clear.
        if not _is_lan_host(parsed.hostname):
            raise ValueError('Plain http is only allowed for LAN hosts')
        log.warning("Application URL uses plain http on a LAN host; reset links travel unencrypted")
    return base.rstrip('/')


def configured_base_url(db):
    configured=db.query(SystemConfig).filter(SystemConfig.key=='base_url').first()
    return configured.value if configured and isinstance(configured.value,str) else ''
