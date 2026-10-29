"""Authenticated same-origin JPEG fallback when WebRTC transport is unavailable."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from core.db import get_db
from core.dependencies import oauth2_scheme
from core.rbac import require_role
from modules.printers.camera_routes import get_camera_stream
from modules.printers.route_utils import mjpeg_stream_name
from modules.printers.go2rtc_access import go2rtc_api_url, go2rtc_client_kwargs

router = APIRouter()
MAX_FRAME_BYTES = 8 * 1024 * 1024


@router.get("/cameras/{printer_id}/snapshot", tags=["Cameras"])
async def camera_snapshot(printer_id: int, db: Session = Depends(get_db),
                          user: dict = Depends(require_role("viewer", scope="read:printers"))):
    # Reuse the same camera-enabled, active, and organization fence as signaling.
    get_camera_stream(printer_id, db, user)
    # Use the existing JPEG variant: an H.264 camera needs on-demand transcoding.
    stream_name = mjpeg_stream_name(printer_id)
    try:
        async with httpx.AsyncClient(trust_env=False, **go2rtc_client_kwargs()) as client:
            async with client.stream("GET", f"{go2rtc_api_url()}/api/frame.jpeg",
                                     params={"src": stream_name}, timeout=8,
                                     follow_redirects=False) as upstream:
                if upstream.status_code != 200:
                    raise HTTPException(502, "Camera snapshot unavailable")
                frame = bytearray()
                async for chunk in upstream.aiter_bytes():
                    frame.extend(chunk)
                    if len(frame) > MAX_FRAME_BYTES:
                        raise HTTPException(502, "Camera snapshot too large")
        if not frame.startswith(b"\xff\xd8\xff"):
            raise HTTPException(502, "Camera snapshot unavailable")
        return Response(bytes(frame), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except httpx.HTTPError:
        raise HTTPException(502, "Camera snapshot unavailable") from None


# Per API worker, shared with OBS: 3 per user/printer and 8 per printer. A printer
# operator may evict the oldest unprivileged viewer when the printer is full.
MJPEG_MAX_STREAMS_PER_USER_PRINTER = 3
MJPEG_MAX_STREAMS_PER_PRINTER = 8
MJPEG_REVALIDATE_SECONDS = 30


def _mjpeg_check(bind, auth_kind, credential, user_id, printer_id):
    """Re-resolve the admission credential in a short worker-thread session."""
    from core.dependencies import resolve_legacy_api_key, resolve_user_api_token, validate_access_token
    from core.rbac import check_org_access
    from modules.printers.camera_routes import _authorize_media
    from modules.printers.models import Printer
    with Session(bind=bind) as db:
        resolver = {
            "session_jwt": validate_access_token,
            "bearer_jwt": validate_access_token,
            "user_api_token": resolve_user_api_token,
            "legacy_global_api_key": resolve_legacy_api_key,
        }.get(auth_kind)
        user = resolver(credential, db) if resolver and credential else None
        if not user or user["id"] != user_id:
            raise HTTPException(401, "Not authenticated")
        _authorize_media(user)
        get_camera_stream(printer_id, db, user)
        printer = db.get(Printer, printer_id)
        return {"user_id": user["id"], "token_id": user.get("_token_id"),
                "privileged": user["role"] in ("admin", "operator")
                and check_org_access(user, printer.org_id)}


@router.get("/cameras/{printer_id}/mjpeg", tags=["Cameras"])
async def camera_mjpeg(printer_id: int, request: Request, db: Session = Depends(get_db),
                       token: str = Depends(oauth2_scheme),
                       user: dict = Depends(require_role("viewer", scope="read:printers"))):
    """Same-origin live fallback with the snapshot route's camera fence."""
    from modules.printers import mjpeg_relay
    from modules.printers.routes import sync_go2rtc_config
    bind = db.get_bind()
    db.close()  # dependency auth is complete; release its connection before streaming
    # Capture only the carrier that authenticated admission. Never fall back to
    # another header after revocation, or retain the request/user in the relay.
    auth_kind, user_id = user.get("_auth_kind"), user["id"]
    if auth_kind == "session_jwt":
        credential = request.cookies.get("session")
    elif auth_kind == "bearer_jwt":
        credential = token
    elif auth_kind in ("user_api_token", "legacy_global_api_key"):
        credential = request.headers.get("X-API-Key")
    else:
        raise HTTPException(401, "Not authenticated")

    def sync():
        with Session(bind=bind) as session:
            sync_go2rtc_config(session)

    def claim(viewer, printer_id):
        return mjpeg_relay.claim_stream(
            mjpeg_relay.STREAMS, viewer, printer_id,
            per_user=MJPEG_MAX_STREAMS_PER_USER_PRINTER,
            per_printer=MJPEG_MAX_STREAMS_PER_PRINTER,
        )

    return await mjpeg_relay.open_mjpeg(
        printer_id, check=lambda: _mjpeg_check(bind, auth_kind, credential, user_id, printer_id),
        sync=sync, claim=claim,
        release=lambda entry: mjpeg_relay.release_stream(mjpeg_relay.STREAMS, entry),
        revalidate_seconds=MJPEG_REVALIDATE_SECONDS,
    )
