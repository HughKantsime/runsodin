"""Authenticated same-origin JPEG fallback when WebRTC transport is unavailable."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session
from core.db import get_db
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
