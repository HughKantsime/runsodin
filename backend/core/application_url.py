"""The application's public URL used in emailed links (password reset).

Admins set it in Settings > Notifications (PUT /api/smtp-config base_url);
links are never built from the request Host header.
"""
from urllib.parse import urlsplit

from core.models import SystemConfig


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
    return base.rstrip('/')


def configured_base_url(db):
    configured=db.query(SystemConfig).filter(SystemConfig.key=='base_url').first()
    return configured.value if configured and isinstance(configured.value,str) else ''
