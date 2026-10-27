"""Read-only remote access settings; no Tailscale state or credentials are read."""
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, Request
from core.config import settings
from core.rbac import require_role

router = APIRouter()


@router.get("/settings/remote-access", tags=["Settings"])
def remote_access_status(request: Request, user: dict = Depends(require_role("admin"))):
    url = settings.odin_public_url
    return {
        "configured": bool(url),
        "url": url or None,
        "tailnet": bool(url and urlsplit(url).hostname.endswith(".ts.net")),
        "current_origin_verified": bool(url and str(request.base_url).rstrip("/") == url),
        "check_url": f"{url}/health" if url else None,
        "camera_transport": "tcp-with-snapshot-fallback" if settings.odin_tailnet_ip else "snapshot-fallback",
    }
