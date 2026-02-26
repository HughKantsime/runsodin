"""
O.D.I.N. — Webhook URL validation helper.

Provides SSRF-safe webhook URL validation used by the notifications
and organizations modules.

Extracted from deps.py as part of the modular architecture refactor.
"""

from fastapi import HTTPException


def _validate_webhook_url(url: str) -> None:
    """Validate a webhook URL is not targeting internal infrastructure (SSRF prevention).

    Allows http:// and https:// schemes only.
    Rejects loopback, link-local, and RFC-1918 private addresses.
    Raises HTTPException 400 if the URL is invalid or targets a blocked host.
    """
    import ipaddress as _ipaddress
    import urllib.parse as _urllib_parse

    if not url:
        return

    try:
        parsed = _urllib_parse.urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook URL")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Webhook URL must use http:// or https:// scheme")

    host = parsed.hostname or ""
    blocked_prefixes = ("localhost", "127.", "169.254.", "0.", "::1")
    if any(host.startswith(p) for p in blocked_prefixes):
        raise HTTPException(status_code=400, detail="Webhook URL targets a blocked host")

    try:
        addr = _ipaddress.ip_address(host)
        if addr.is_loopback or addr.is_link_local or addr.is_private:
            raise HTTPException(status_code=400, detail="Webhook URL targets a blocked host")
    except ValueError:
        pass  # hostname — allow (DNS resolution happens at dispatch time)
