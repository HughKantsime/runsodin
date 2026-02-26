"""O.D.I.N. — Camera & Timelapse Routes

Timelapse list/video/delete, camera list/toggle/stream/WebRTC.
"""

# Domain: printers
# Depends on: core, organizations
# Owns tables: timelapses (shared with archives)

from datetime import datetime
from pathlib import Path
from typing import Optional
import logging
import os
import shutil
import subprocess
import tempfile

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
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
    video_path = Path(os.path.realpath(Path("/data/timelapses") / t.filename))
    if not str(video_path).startswith("/data/timelapses/"):
        raise HTTPException(status_code=404, detail="Not found")
    if not video_path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found")
    return FileResponse(str(video_path), media_type="video/mp4", filename=video_path.name)


@router.delete("/timelapses/{timelapse_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Timelapses"])
def delete_timelapse(timelapse_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Delete a timelapse recording and its video file."""
    t = db.query(Timelapse).filter(Timelapse.id == timelapse_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Timelapse not found")
    # Delete video file
    video_path = Path(os.path.realpath(Path("/data/timelapses") / t.filename))
    if not str(video_path).startswith("/data/timelapses/"):
        raise HTTPException(status_code=400, detail="Invalid file path")
    if video_path.exists():
        video_path.unlink()
    # Delete frame directory if still around
    frame_dir = Path("/data/timelapses") / str(t.id)
    if frame_dir.exists():
        shutil.rmtree(str(frame_dir), ignore_errors=True)
    db.delete(t)
    db.commit()
    log_audit(db, "delete", "timelapse", t.id, {"printer_id": t.printer_id})


def _check_ffmpeg() -> bool:
    """Check if ffmpeg is available."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)  # noqa: S603 S607
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _get_timelapse_path(t: Timelapse) -> Path:
    """Resolve and validate timelapse video path."""
    video_path = Path(os.path.realpath(Path("/data/timelapses") / t.filename))
    if not str(video_path).startswith("/data/timelapses/"):
        raise HTTPException(status_code=404, detail="Not found")
    if not video_path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found")
    return video_path


@router.get("/timelapses/{timelapse_id}/stream", tags=["Timelapses"])
def stream_timelapse(
    timelapse_id: int,
    token: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Stream timelapse video for in-browser playback (no Content-Disposition)."""
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
    video_path = _get_timelapse_path(t)
    return FileResponse(str(video_path), media_type="video/mp4")


@router.get("/timelapses/{timelapse_id}/download", tags=["Timelapses"])
def download_timelapse(
    timelapse_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Download timelapse as attachment."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    t = db.query(Timelapse).filter(Timelapse.id == timelapse_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Timelapse not found")
    if t.status != "ready":
        raise HTTPException(status_code=400, detail=f"Timelapse is {t.status}, not ready")
    video_path = _get_timelapse_path(t)
    safe_name = f"odin_timelapse_{t.id}.mp4"
    return FileResponse(
        str(video_path),
        media_type="video/mp4",
        filename=safe_name,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.post("/timelapses/{timelapse_id}/trim", tags=["Timelapses"])
def trim_timelapse(
    timelapse_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator")),
):
    """Trim timelapse to start/end points using ffmpeg. Overwrites original."""
    if not _check_ffmpeg():
        raise HTTPException(status_code=501, detail="ffmpeg is not installed — timelapse editing is unavailable")
    start_seconds = body.get("start_seconds", 0)
    end_seconds = body.get("end_seconds")
    if end_seconds is None:
        raise HTTPException(status_code=400, detail="end_seconds is required")
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise HTTPException(status_code=400, detail="Invalid time range")

    t = db.query(Timelapse).filter(Timelapse.id == timelapse_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Timelapse not found")
    if t.status != "ready":
        raise HTTPException(status_code=400, detail=f"Timelapse is {t.status}, not ready")
    video_path = _get_timelapse_path(t)

    with tempfile.NamedTemporaryFile(suffix=".mp4", dir=video_path.parent, delete=False) as tmp:
        tmp_path = tmp.name
    try:
        cmd = [
            "ffmpeg", "-y", "-ss", str(start_seconds), "-to", str(end_seconds),
            "-i", str(video_path), "-c", "copy", tmp_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300)  # noqa: S603
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail="ffmpeg trim failed")
        os.replace(tmp_path, str(video_path))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="ffmpeg timed out")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    new_duration = end_seconds - start_seconds
    t.duration_seconds = new_duration
    file_stat = video_path.stat()
    t.file_size_mb = round(file_stat.st_size / (1024 * 1024), 2)
    db.commit()
    log_audit(db, "update", "timelapse", t.id, {"action": "trim", "start": start_seconds, "end": end_seconds})

    return {
        "id": t.id, "duration_seconds": t.duration_seconds,
        "file_size_mb": t.file_size_mb, "status": t.status,
    }


VALID_SPEED_MULTIPLIERS = {0.5, 1.0, 1.5, 2.0, 4.0, 8.0}


@router.post("/timelapses/{timelapse_id}/speed", tags=["Timelapses"])
def speed_timelapse(
    timelapse_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator")),
):
    """Create speed-adjusted copy of timelapse using ffmpeg."""
    if not _check_ffmpeg():
        raise HTTPException(status_code=501, detail="ffmpeg is not installed — timelapse editing is unavailable")
    multiplier = body.get("multiplier")
    if multiplier is None or float(multiplier) not in VALID_SPEED_MULTIPLIERS:
        raise HTTPException(status_code=400, detail=f"multiplier must be one of {sorted(VALID_SPEED_MULTIPLIERS)}")
    multiplier = float(multiplier)

    t = db.query(Timelapse).filter(Timelapse.id == timelapse_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Timelapse not found")
    if t.status != "ready":
        raise HTTPException(status_code=400, detail=f"Timelapse is {t.status}, not ready")
    video_path = _get_timelapse_path(t)

    speed_label = str(multiplier).replace(".", "_")
    out_name = f"{video_path.stem}_{speed_label}x.mp4"
    out_path = video_path.parent / out_name
    setpts = round(1.0 / multiplier, 4)

    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"setpts={setpts}*PTS", "-an", str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=600)  # noqa: S603
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail="ffmpeg speed adjustment failed")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="ffmpeg timed out")

    file_stat = out_path.stat()
    new_duration = (t.duration_seconds or 0) / multiplier
    new_tl = Timelapse(
        printer_id=t.printer_id,
        print_job_id=t.print_job_id,
        filename=str(Path(t.filename).parent / out_name),
        frame_count=t.frame_count,
        duration_seconds=round(new_duration, 1),
        file_size_mb=round(file_stat.st_size / (1024 * 1024), 2),
        status="ready",
        created_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
    )
    db.add(new_tl)
    db.commit()
    db.refresh(new_tl)
    log_audit(db, "create", "timelapse", new_tl.id, {"action": "speed", "multiplier": multiplier, "source_id": t.id})

    return {
        "id": new_tl.id, "duration_seconds": new_tl.duration_seconds,
        "file_size_mb": new_tl.file_size_mb, "status": new_tl.status,
        "filename": new_tl.filename,
    }


# ====================================================================
# Cameras
# ====================================================================

@router.get("/cameras", tags=["Cameras"])
def list_cameras(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
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

    printers = db.query(Printer).filter(Printer.is_active.is_(True), Printer.camera_enabled.is_(True)).all()
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
def get_camera_stream(printer_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
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
async def camera_webrtc(printer_id: int, request: Request, db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
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
