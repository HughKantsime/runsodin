"""O.D.I.N. — Camera & Timelapse Routes

Timelapse list/video/delete, camera list/toggle/stream/WebRTC.
"""

from datetime import datetime
from pathlib import Path
from typing import Optional
import logging
import shutil

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import decode_token
from deps import get_db, get_current_user, require_role, log_audit
from models import Printer, Timelapse
from routers.printers import get_camera_url, sync_go2rtc_config

log = logging.getLogger("odin.api")
router = APIRouter()


# ====================================================================
# Timelapses
# ====================================================================

@router.get("/timelapses", tags=["Timelapses"])
def list_timelapses(
    printer_id: Optional[int] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """List timelapse recordings with optional filters."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    q = db.query(Timelapse).order_by(Timelapse.created_at.desc())
    if printer_id:
        q = q.filter(Timelapse.printer_id == printer_id)
    if status_filter:
        q = q.filter(Timelapse.status == status_filter)
    total = q.count()
    items = q.offset(offset).limit(limit).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "timelapses": [
            {
                "id": t.id,
                "printer_id": t.printer_id,
                "printer_name": t.printer.name if t.printer else None,
                "print_job_id": t.print_job_id,
                "filename": t.filename,
                "frame_count": t.frame_count,
                "duration_seconds": t.duration_seconds,
                "file_size_mb": t.file_size_mb,
                "status": t.status,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in items
        ],
    }


@router.get("/timelapses/{timelapse_id}/video", tags=["Timelapses"])
def get_timelapse_video(timelapse_id: int, token: Optional[str] = None, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Serve the timelapse MP4 file. Accepts ?token= query param for <video> src auth."""
    # <video> elements can't send Bearer headers, so accept token as query param
    if not current_user and token:
        token_data = decode_token(token)
        if token_data:
            user = db.execute(text("SELECT * FROM users WHERE username = :username"),
                              {"username": token_data.username}).fetchone()
            if user:
                current_user = dict(user._mapping)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    t = db.query(Timelapse).filter(Timelapse.id == timelapse_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Timelapse not found")
    if t.status != "ready":
        raise HTTPException(status_code=400, detail=f"Timelapse is {t.status}, not ready")
    video_path = Path("/data/timelapses") / t.filename
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")
    return FileResponse(str(video_path), media_type="video/mp4", filename=video_path.name)


@router.delete("/timelapses/{timelapse_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Timelapses"])
def delete_timelapse(timelapse_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Delete a timelapse recording and its video file."""
    t = db.query(Timelapse).filter(Timelapse.id == timelapse_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Timelapse not found")
    # Delete video file
    video_path = Path("/data/timelapses") / t.filename
    if video_path.exists():
        video_path.unlink()
    # Delete frame directory if still around
    frame_dir = Path("/data/timelapses") / str(t.id)
    if frame_dir.exists():
        shutil.rmtree(str(frame_dir), ignore_errors=True)
    db.delete(t)
    db.commit()
    log_audit(db, "delete", "timelapse", t.id, {"printer_id": t.printer_id})


# ====================================================================
# Cameras
# ====================================================================

@router.get("/cameras", tags=["Cameras"])
def list_cameras(db: Session = Depends(get_db)):
    """List printers with active camera streams in go2rtc."""
    # Check which streams go2rtc actually has configured
    active_streams = set()
    try:
        resp = httpx.get("http://127.0.0.1:1984/api/streams", timeout=2.0)
        if resp.status_code == 200:
            streams = resp.json()
            for key in streams:
                # Stream names are "printer_{id}"
                if key.startswith("printer_"):
                    try:
                        active_streams.add(int(key.split("_")[1]))
                    except ValueError:
                        pass
    except Exception:
        pass

    printers = db.query(Printer).filter(Printer.is_active == True, Printer.camera_enabled == True).all()
    cameras = []
    for p in printers:
        if p.id in active_streams:
            cameras.append({"id": p.id, "name": p.name, "has_camera": True, "display_order": p.display_order or 0, "camera_enabled": bool(p.camera_enabled)})
    return sorted(cameras, key=lambda x: x.get("display_order", 0))


@router.patch("/cameras/{printer_id}/toggle", tags=["Cameras"])
def toggle_camera(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Toggle camera on/off for a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Toggle the camera_enabled flag
    new_state = not (printer.camera_enabled if printer.camera_enabled is not None else True)
    db.execute(text("UPDATE printers SET camera_enabled = :enabled WHERE id = :id"),
               {"enabled": new_state, "id": printer_id})
    db.commit()

    return {"id": printer_id, "camera_enabled": new_state}


@router.get("/cameras/{printer_id}/stream", tags=["Cameras"])
def get_camera_stream(printer_id: int, db: Session = Depends(get_db)):
    """Get go2rtc stream info for a printer camera."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    camera_url = get_camera_url(printer)
    if not camera_url:
        raise HTTPException(status_code=404, detail="Camera not found")

    stream_name = f"printer_{printer_id}"

    # Ensure go2rtc config is up to date
    sync_go2rtc_config(db)

    return {
        "printer_id": printer_id,
        "printer_name": printer.name,
        "stream_name": stream_name,
        "webrtc_url": f"/api/cameras/{printer_id}/webrtc",
    }


@router.post("/cameras/{printer_id}/webrtc", tags=["Cameras"])
async def camera_webrtc(printer_id: int, request: Request, db: Session = Depends(get_db)):
    """Proxy WebRTC signaling to go2rtc."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    camera_url = get_camera_url(printer)
    if not camera_url:
        raise HTTPException(status_code=404, detail="Camera not found")

    stream_name = f"printer_{printer_id}"
    body = await request.body()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"http://127.0.0.1:1984/api/webrtc?src={stream_name}",
            content=body,
            headers={"Content-Type": request.headers.get("content-type", "application/sdp")},
        )

    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/sdp"))
