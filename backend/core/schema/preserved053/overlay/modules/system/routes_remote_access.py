"""Read-only remote access settings; no Tailscale state or credentials are read."""
import re
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, Request
from core.config import settings
from core.rbac import require_role

router = APIRouter()


def _display_origin(value: str) -> str | None:
    """Expose only HTTPS DNS origins, never transport or trusted-proxy addresses."""
    from core.remote_access import validate_public_origin
    try:
        origin = validate_public_origin(value)
        host = urlsplit(origin).hostname or ""
        labels = host.split(".")
        if len(labels) < 2 or not re.fullmatch(r"[a-z][a-z0-9-]*", labels[-1]):
            return None
        if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
            return None
        return origin
    except (TypeError, ValueError):
        return None


@router.get("/settings/remote-access", tags=["Settings"])
def remote_access_status(request: Request, user: dict = Depends(require_role("admin"))):
    url = _display_origin(settings.odin_public_url)
    return {
        "configured": bool(settings.odin_public_url),
        "url": url or None,
        "tailnet": bool(url and urlsplit(url).hostname.endswith(".ts.net")),
        "current_origin_verified": bool(url and str(request.base_url).rstrip("/") == url),
        "check_url": f"{url}/health" if url else None,
        "tailnet_ice_configured": bool(settings.odin_tailnet_ip),
        "camera_transport": "tcp-with-snapshot-fallback" if settings.odin_tailnet_ip else "snapshot-fallback",
    }
