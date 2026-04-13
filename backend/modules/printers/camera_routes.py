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

from core.db import get_db
from core.dependencies import get_current_user, log_audit, validate_access_token
from core.rbac import require_role, check_org_access
from modules.printers.models import Printer
from modules.archives.models import Timelapse
from modules.printers.routes import get_camera_url, sync_go2rtc_config

log = logging.getLogger("odin.api")
router = APIRouter()


def _check_printer_org(current_user: dict, printer_id: int, db) -> None:
    """Verify the caller has org access to the printer behind a timelapse/camera."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if printer and not check_org_access(current_user, printer.org_id) and not printer.shared:
        raise HTTPException(status_code=404, detail="Not found")


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
    current_user: dict = Depends(require_role("viewer")),
):
    """List timelapse recordings with optional filters."""
    from core.rbac import get_org_scope
    org = get_org_scope(current_user)
    q = db.query(Timelapse).order_by(Timelapse.created_at.desc())
    if org is not None:
        q = q.join(Printer, Timelapse.printer_id == Printer.id).filter(
            (Printer.org_id == org) | (Printer.org_id == None) | (Printer.shared == True)
        )
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


@router.get("/timelapses/{timelapse_id}", tags=["Timelapses"])
def get_timelapse(
    timelapse_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer")),
):
    """Get a single timelapse by ID."""
    t = db.query(Timelapse).filter(Timelapse.id == timelapse_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Timelapse not found")
    _check_printer_org(current_user, t.printer_id, db)
    return {
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


@router.get("/timelapses/{timelapse_id}/video", tags=["Timelapses"])
def get_timelapse_video(timelapse_id: int, token: Optional[str] = None, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Serve the timelapse MP4 file. Accepts ?token= query param for <video> src auth."""
    # <video> elements can't send Bearer headers, so accept token as query param.
    # Goes through the SAME validation as get_current_user: blacklist check,
    # rejects ws/mfa_pending/mfa_setup_required tokens.
    if not current_user and token:
        current_user = validate_access_token(token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    t = db.query(Timelapse).filter(Timelapse.id == timelapse_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Timelapse not found")
    _check_printer_org(current_user, t.printer_id, db)
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
    _check_printer_org(current_user, t.printer_id, db)
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
    log_audit(db, "delete", "timelapse", t.id, {"printer_id": t.printer_id})
    db.commit()


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
    # Same query-param token flow as /video — use full validator.
    if not current_user and token:
        current_user = validate_access_token(token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    t = db.query(Timelapse).filter(Timelapse.id == timelapse_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Timelapse not found")
    _check_printer_org(current_user, t.printer_id, db)
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
    _check_printer_org(current_user, t.printer_id, db)
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
    _check_printer_org(current_user, t.printer_id, db)
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
    log_audit(db, "update", "timelapse", t.id, {"action": "trim", "start": start_seconds, "end": end_seconds})
    db.commit()

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
    _check_printer_org(current_user, t.printer_id, db)
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
    db.flush()
    db.refresh(new_tl)
    log_audit(db, "create", "timelapse", new_tl.id, {"action": "speed", "multiplier": multiplier, "source_id": t.id})
    db.commit()

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
    """List printers with active camera streams.

    Returns cameras based on DB state (printers with camera URLs).
    go2rtc streams are synced lazily — if no streams exist yet, we
    trigger a config sync but don't block the response on it.
    """
    from core.rbac import get_org_scope
    org = get_org_scope(current_user)
    pq = db.query(Printer).filter(Printer.is_active.is_(True), Printer.camera_enabled.is_(True))
    if org is not None:
        pq = pq.filter((Printer.org_id == org) | (Printer.org_id == None) | (Printer.shared == True))
    printers = pq.all()

    # Build camera list from DB — don't gate on go2rtc being live
    cameras = []
    for p in printers:
        url = get_camera_url(p)
        if url:
            cameras.append({"id": p.id, "name": p.name, "has_camera": True, "display_order": p.display_order or 0, "camera_enabled": bool(p.camera_enabled)})

    # Ensure go2rtc config is synced (no-op if already up to date)
    if cameras:
        sync_go2rtc_config(db)

    return sorted(cameras, key=lambda x: x.get("display_order", 0))


@router.patch("/cameras/{printer_id}/toggle", tags=["Cameras"])
def toggle_camera(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Toggle camera on/off for a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id) and not printer.shared:
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
    if not check_org_access(current_user, printer.org_id) and not printer.shared:
        raise HTTPException(status_code=404, detail="Printer not found")

    camera_url = get_camera_url(printer)
    if not camera_url:
        raise HTTPException(status_code=404, detail="Camera not found")

    stream_name = f"printer_{printer_id}"

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
    if not check_org_access(current_user, printer.org_id) and not printer.shared:
        raise HTTPException(status_code=404, detail="Printer not found")

    camera_url = get_camera_url(printer)
    if not camera_url:
        raise HTTPException(status_code=404, detail="Camera not found")

    stream_name = f"printer_{printer_id}"
    body = await request.body()

    # Ensure go2rtc has this stream configured before proxying.
    # Only sync if the stream is genuinely missing — never restart just
    # because go2rtc is temporarily unreachable (e.g. mid-restart).
    try:
        async with httpx.AsyncClient() as client:
            streams_resp = await client.get(
                "http://127.0.0.1:1984/api/streams", timeout=2.0,
            )
            if streams_resp.status_code == 200:
                streams = streams_resp.json()
                if stream_name not in streams:
                    sync_go2rtc_config(db)
    except Exception:
        # go2rtc not reachable — write config but don't force a restart
        # (it may already be restarting). The client will retry.
        sync_go2rtc_config(db)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:1984/api/webrtc?src={stream_name}",
                content=body,
                headers={"Content-Type": request.headers.get("content-type", "application/sdp")},
                timeout=5.0,
            )
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"go2rtc returned {resp.status_code}")
        return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/sdp"))
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="go2rtc is not reachable")
    except (httpx.TimeoutException, httpx.RemoteProtocolError):
        raise HTTPException(status_code=504, detail="go2rtc not ready — try again")
