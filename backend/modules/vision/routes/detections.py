"""O.D.I.N. — Vigil AI: Detections, Settings, Frames, Stats, and Training Data."""

from core.rbac import should_filter_org, require_action

# Domain: vision
# Depends on: core, printers, notifications, organizations
# Owns tables: vision_detections, vision_settings

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func, case, or_, text
from typing import Optional
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re

from core.db import get_db
from core.rbac import require_role, require_superadmin, get_org_scope, check_org_access
from core.config import settings
from core.models import SystemConfig
from modules.printers.models import Printer
from modules.vision.models import VisionDetection, VisionSettings, VisionModel

log = logging.getLogger("odin.api")
router = APIRouter()


def _detection_to_dict(det: VisionDetection, printer_name=None, printer_nickname=None) -> dict:
    """Convert a VisionDetection ORM object to a dict matching the raw sqlite3 output."""
    d = {
        "id": det.id,
        "printer_id": det.printer_id,
        "print_job_id": det.print_job_id,
        "detection_type": det.detection_type,
        "confidence": det.confidence,
        "status": det.status,
        "frame_path": det.frame_path,
        "bbox_json": det.bbox_json,
        "metadata_json": det.metadata_json,
        "reviewed_by": det.reviewed_by,
        "reviewed_at": det.reviewed_at.isoformat() if det.reviewed_at else None,
        "created_at": det.created_at.isoformat() if det.created_at else None,
    }
    if printer_name is not None:
        d["printer_name"] = printer_name
    if printer_nickname is not None:
        d["printer_nickname"] = printer_nickname
    return d


def _settings_to_dict(s: VisionSettings) -> dict:
    """Convert a VisionSettings ORM object to a dict."""
    return {
        "printer_id": s.printer_id,
        "enabled": s.enabled,
        "spaghetti_enabled": s.spaghetti_enabled,
        "spaghetti_threshold": s.spaghetti_threshold,
        "first_layer_enabled": s.first_layer_enabled,
        "first_layer_threshold": s.first_layer_threshold,
        "detachment_enabled": s.detachment_enabled,
        "detachment_threshold": s.detachment_threshold,
        "build_plate_empty_enabled": s.build_plate_empty_enabled,
        "build_plate_empty_threshold": s.build_plate_empty_threshold,
        "auto_pause": s.auto_pause,
        "capture_interval_sec": s.capture_interval_sec,
        "collect_training_data": s.collect_training_data,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


# ============== Vigil AI: Detections ==============

@router.get("/vision/detections", tags=["Vigil AI"])
async def list_vision_detections(
    printer_id: Optional[int] = None,
    detection_type: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, le=200),
    offset: int = 0,
    current_user: dict = Depends(require_role("viewer",scope="read")),
    db: Session = Depends(get_db),
):
    """Legacy detection ownership is unknown, not today's printer audience."""
    from modules.vision.training_service import maintenance
    if not maintenance(current_user):return {"items":[],"total":0}
    org = get_org_scope(current_user)
    query = (
        db.query(VisionDetection, Printer.name, Printer.nickname)
        .outerjoin(Printer, Printer.id == VisionDetection.printer_id)
    )
    count_query = db.query(sa_func.count(VisionDetection.id)).outerjoin(Printer, Printer.id == VisionDetection.printer_id)

    if should_filter_org(current_user, org):
        org_filter = (Printer.org_id == org) | (Printer.org_id == None) | (Printer.shared == True)
        query = query.filter(org_filter)
        count_query = count_query.filter(org_filter)

    if printer_id is not None:
        query = query.filter(VisionDetection.printer_id == printer_id)
        count_query = count_query.filter(VisionDetection.printer_id == printer_id)
    if detection_type:
        query = query.filter(VisionDetection.detection_type == detection_type)
        count_query = count_query.filter(VisionDetection.detection_type == detection_type)
    if status_filter:
        query = query.filter(VisionDetection.status == status_filter)
        count_query = count_query.filter(VisionDetection.status == status_filter)
    else:
        # Collected training samples are not detections; list them only on request.
        query = query.filter(_not_training())
        count_query = count_query.filter(_not_training())

    total = count_query.scalar()
    rows = (
        query.order_by(VisionDetection.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    items = [
        _detection_to_dict(det, printer_name=pname, printer_nickname=pnick)
        for det, pname, pnick in rows
    ]
    return {"items": items, "total": total}


@router.get("/vision/detections/{detection_id}", tags=["Vigil AI"])
async def get_vision_detection(
    detection_id: int,
    current_user: dict = Depends(require_role("viewer",scope="read")),
    db: Session = Depends(get_db),
):
    from modules.vision.training_service import maintenance
    if not maintenance(current_user):raise HTTPException(404,"Detection not found")
    row = (
        db.query(VisionDetection, Printer.name, Printer.nickname, Printer.org_id)
        .outerjoin(Printer, Printer.id == VisionDetection.printer_id)
        .filter(VisionDetection.id == detection_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Detection not found")
    det, pname, pnick, printer_org_id = row
    if not check_org_access(current_user, printer_org_id):
        raise HTTPException(status_code=404, detail="Detection not found")
    return _detection_to_dict(det, printer_name=pname, printer_nickname=pnick)


@router.patch("/vision/detections/{detection_id}", tags=["Vigil AI"])
async def review_vision_detection(
    detection_id: int,
    request: Request,
    current_user: dict = Depends(require_role("operator",scope="write")),
    db: Session = Depends(get_db),
):
    from modules.vision.training_service import maintenance
    from sqlalchemy import update
    if not maintenance(current_user):raise HTTPException(404,"Detection not found")
    body=await request.json()
    if not isinstance(body,dict) or body.get("status") not in ("confirmed","dismissed"):
        raise HTTPException(400,"Status must be confirmed or dismissed")
    det=db.query(VisionDetection).filter(VisionDetection.id==detection_id).first()
    if det is None:raise HTTPException(404,"Detection not found")
    if det.status=='training':raise HTTPException(409,"Training samples are labeled, not reviewed")
    statement=update(VisionDetection).where(VisionDetection.id==detection_id,
        VisionDetection.printer_id==det.printer_id,VisionDetection.print_job_id==det.print_job_id,
        VisionDetection.frame_path==det.frame_path,VisionDetection.status==det.status,
        VisionDetection.reviewed_by==det.reviewed_by,VisionDetection.reviewed_at==det.reviewed_at
    ).values(status=body["status"],reviewed_by=current_user["id"],reviewed_at=sa_func.now())
    try:
        changed=db.execute(statement.execution_options(synchronize_session=False)).rowcount
        if changed!=1:
            db.rollback();raise HTTPException(409,"Detection changed; refresh before reviewing")
        db.commit()
    except HTTPException:raise
    except Exception:
        try:db.rollback()
        except Exception:pass
        raise HTTPException(503,"Review persistence unconfirmed; refresh before retrying") from None
    return {"id":detection_id,"status":body["status"]}


@router.post("/vision/detections/{detection_id}/dismiss-and-resume", tags=["Vigil AI"])
# Sync handler: the fenced DB transaction and the bounded monitor socket call block.
def dismiss_and_resume(
    detection_id: int,
    current_user: dict = Depends(require_role("operator",scope="write")),
    db: Session = Depends(get_db),
):
    from modules.vision.training_service import maintenance
    if not maintenance(current_user):raise HTTPException(404,"Detection not found")
    from sqlalchemy import update
    # Serialize source facts, including observation inserts, through transport.
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '4s'"))
            db.execute(text("LOCK TABLE printers, print_jobs, vision_detections IN SHARE ROW EXCLUSIVE MODE"))
        else:
            db.execute(update(VisionDetection).where(VisionDetection.id == detection_id)
                       .values(status=VisionDetection.status).execution_options(synchronize_session=False))
        db.expire_all()
        det = db.query(VisionDetection).filter(VisionDetection.id == detection_id).first()
        if det is None:
            raise HTTPException(404, "Detection not found")
        printer = db.query(Printer).filter(Printer.id == det.printer_id).first()
        if printer is None or not check_org_access(current_user, printer.org_id):
            raise HTTPException(404, "Printer not found")
        jobs = db.execute(text("SELECT id FROM print_jobs WHERE printer_id=:pid AND status='running' AND ended_at IS NULL"),
                          {"pid": printer.id}).scalars().all()
        state = printer.gcode_state
        if (not printer.is_active or det.status == 'training' or
                type(det.print_job_id) is not int or det.print_job_id <= 0 or
                jobs != [det.print_job_id] or state not in ('PAUSE', 'PAUSED', 'RUNNING')):
            raise HTTPException(409, "Detection does not match the sole current paused/running task")
        statement = update(VisionDetection).where(
            VisionDetection.id == detection_id, VisionDetection.printer_id == det.printer_id,
            VisionDetection.print_job_id == det.print_job_id, VisionDetection.frame_path == det.frame_path,
            VisionDetection.status == det.status, VisionDetection.reviewed_by == det.reviewed_by,
            VisionDetection.reviewed_at == det.reviewed_at).values(
                status='dismissed', reviewed_by=current_user['id'], reviewed_at=sa_func.now())
        if db.execute(statement.execution_options(synchronize_session=False)).rowcount != 1:
            raise HTTPException(409, "Detection changed; refresh before reviewing")
        outcome = 'not_needed'
        if state in ('PAUSE', 'PAUSED'):
            from modules.printers.services import request_observed_task_resume
            outcome = request_observed_task_resume(printer, det.print_job_id)
            if outcome != 'accepted':
                raise HTTPException(503 if outcome != 'refused' else 409,
                                    detail={'outcome': outcome, 'message': 'Resume unconfirmed; refresh before further action'})
        db.commit()
        return {'id': detection_id, 'detection_id': detection_id, 'status': 'dismissed', 'outcome': outcome,
                'actions': ['printer:resume_requested' if outcome == 'accepted' else 'printer:already_running'],
                'resume_requested': outcome == 'accepted'}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(503, detail={'outcome': 'unknown',
            'message': 'Dismiss/resume outcome unknown; refresh before further action'}) from None


# ============== Vigil AI: Per-Printer Vision Settings ==============

@router.get("/printers/{printer_id}/vision", tags=["Vigil AI"])
async def get_printer_vision_settings(
    printer_id: int,
    current_user: dict = Depends(require_role("viewer", scope="read:printers")),
    db: Session = Depends(get_db),
):
    """Get per-printer vision settings."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")

    row = db.query(VisionSettings).filter(VisionSettings.printer_id == printer_id).first()
    if row:
        return _settings_to_dict(row)
    # Return defaults
    return {
        "printer_id": printer_id,
        "enabled": 1,
        "spaghetti_enabled": 1, "spaghetti_threshold": 0.65,
        "first_layer_enabled": 1, "first_layer_threshold": 0.60,
        "detachment_enabled": 1, "detachment_threshold": 0.70,
        "build_plate_empty_enabled": 0, "build_plate_empty_threshold": 0.70,
        "auto_pause": 0, "capture_interval_sec": 10,
        "collect_training_data": 0,
    }


@router.patch("/printers/{printer_id}/vision", tags=["Vigil AI"])
async def update_printer_vision_settings(
    printer_id: int,
    request: Request,
    current_user: dict = Depends(require_action("settings.edit", scope="write:printers")),
    db: Session = Depends(get_db),
):
    """Update per-printer vision settings."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")

    body = await request.json()
    allowed = {
        "enabled", "spaghetti_enabled", "spaghetti_threshold",
        "first_layer_enabled", "first_layer_threshold",
        "detachment_enabled", "detachment_threshold",
        "build_plate_empty_enabled", "build_plate_empty_threshold",
        "auto_pause", "capture_interval_sec", "collect_training_data",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    for key in ("enabled", "spaghetti_enabled", "first_layer_enabled", "detachment_enabled",
                "build_plate_empty_enabled", "auto_pause", "collect_training_data"):
        # INTEGER 0/1 columns on both dialects: JSON booleans are stored as 1/0; anything
        # else is refused (SQLite would store it as is, PostgreSQL would fail the write).
        if key in updates:
            value = updates[key]
            if value is True or value is False or (type(value) is int and value in (0, 1)):
                updates[key] = int(value)
            else:
                raise HTTPException(status_code=422, detail=f"{key} must be true/false or 0/1")
    for key, value in updates.items():
        if key.endswith("_threshold") and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1
        ):
            raise HTTPException(status_code=422, detail=f"{key} must be a number between 0 and 1")
    if "capture_interval_sec" in updates:
        MAX_CAPTURE_INTERVAL = 86400  # mirrors detection_thread.MAX_CAPTURE_INTERVAL (no cv2 import here)
        interval = updates["capture_interval_sec"]
        if type(interval) is not int or not 1 <= interval <= MAX_CAPTURE_INTERVAL:
            raise HTTPException(status_code=422, detail=f"capture_interval_sec must be a whole number from 1 to {MAX_CAPTURE_INTERVAL}")

    row = db.query(VisionSettings).filter(VisionSettings.printer_id == printer_id).first()
    if row:
        for k, v in updates.items():
            setattr(row, k, v)
        row.updated_at = sa_func.now()
    else:
        row = VisionSettings(printer_id=printer_id, **updates)
        db.add(row)

    db.commit()
    return {"printer_id": printer_id, **updates}


# ============== Vigil AI: Global Vision Settings ==============

@router.get("/vision/settings", tags=["Vigil AI"])
async def get_global_vision_settings(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Get global vision settings."""
    row = db.query(SystemConfig).filter(SystemConfig.key == "vision_settings").first()
    defaults = {"enabled": True, "retention_days": 30}
    if row:
        try:
            return {**defaults, **(json.loads(row.value) if isinstance(row.value, str) else row.value)}
        except Exception as e:
            log.debug(f"Failed to parse vision settings: {e}")
    return defaults


@router.patch("/vision/settings", tags=["Vigil AI"])
async def update_global_vision_settings(
    request: Request,
    # One install-wide row: org-scoped admins may not change it.
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db),
):
    """Update global vision settings."""
    body = await request.json()
    allowed = {"enabled", "retention_days", "max_threads"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if "max_threads" in updates and (type(updates["max_threads"]) is not int or not 1 <= updates["max_threads"] <= 256):
        # mirrors detection_thread.MAX_VISION_THREADS_LIMIT (no cv2 import here)
        raise HTTPException(status_code=422, detail="max_threads must be a whole number from 1 to 256")
    if "enabled" in updates and not isinstance(updates["enabled"], bool):
        raise HTTPException(status_code=422, detail="enabled must be a boolean")
    if "retention_days" in updates and (
        isinstance(updates["retention_days"], bool)
        or not isinstance(updates["retention_days"], int)
        or updates["retention_days"] < 1
    ):
        raise HTTPException(status_code=422, detail="retention_days must be a positive integer")

    row = db.query(SystemConfig).filter(SystemConfig.key == "vision_settings").first()
    if row:
        existing = json.loads(row.value) if isinstance(row.value, str) else (row.value or {})
        # Assign a new object: mutating the loaded JSON in place is invisible to SQLAlchemy.
        row.value = {**existing, **updates}
    else:
        db.add(SystemConfig(key="vision_settings", value=updates))
    db.commit()
    return updates


@router.get("/vision/printers", tags=["Vigil AI"])
async def list_vision_camera_printers(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Printers with a camera for Vigil AI (stored URL or auto-derived), for per-printer tuning.
    Same effective-camera rule as the monitor (modules.vision.camera_source)."""
    from modules.vision.camera_source import camera_source
    result = []
    for printer in db.query(Printer).filter(Printer.is_active.is_(True), Printer.camera_enabled.is_(True)).order_by(Printer.id).all():
        if not check_org_access(current_user, printer.org_id):
            continue
        source = camera_source(printer)
        if source:
            result.append({"printer_id": printer.id, "camera_source": source})
    return result


@router.get("/vision/monitor-status", tags=["Vigil AI"])
async def get_vision_monitor_status(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Concurrent vision-thread cap as last reported by the monitor daemon.

    capped_printer_ids lists printers that want monitoring but wait for a free slot
    (limited to printers the caller can see; capped_count is install-wide).
    """
    def _json(row):
        value = row.value if row else None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        return value if isinstance(value, dict) else {}
    settings = _json(db.query(SystemConfig).filter(SystemConfig.key == "vision_settings").first())
    status = _json(db.query(SystemConfig).filter(SystemConfig.key == "vision_monitor_status").first())
    configured = settings.get("max_threads")
    configured = configured if type(configured) is int and 1 <= configured <= 256 else 16
    capped = [pid for pid in status.get("capped_printer_ids", []) if type(pid) is int]
    visible = []
    if capped:
        for printer in db.query(Printer).filter(Printer.id.in_(capped)).all():
            if check_org_access(current_user, printer.org_id):
                visible.append(printer.id)
    return {
        "max_threads": configured,
        "reported": bool(status),
        "active_threads": status.get("active_threads"),
        "capped": bool(capped),
        "capped_count": len(capped),
        "capped_printer_ids": sorted(visible),
    }


def _not_training():
    # NULL status is a legacy detection, not a training sample; NULL != x is never true in SQL.
    return or_(VisionDetection.status.is_(None), VisionDetection.status != 'training')


# ============== Vigil AI: Frames ==============

def _frame_response(current_user, printer_id, relative):
    from fastapi.responses import Response
    from modules.vision.training_service import maintenance,snapshot
    # Current printer membership cannot establish historical image ownership.
    if not maintenance(current_user):raise HTTPException(404,"Frame not found")
    try:
        data,_,_,_=snapshot(printer_id,relative)
    except (ValueError,OSError):raise HTTPException(404,"Frame not found") from None
    return Response(data,media_type="image/jpeg")


@router.get("/vision/frames/{printer_id}/training/{filename}", tags=["Vigil AI"])
async def serve_training_frame(
    printer_id: int,
    filename: str,
    current_user: dict = Depends(require_role("viewer",scope="read")),
    db: Session = Depends(get_db),
):
    return _frame_response(current_user, printer_id, f"{printer_id}/training/{filename}")


@router.get("/vision/frames/{printer_id}/{filename}", tags=["Vigil AI"])
async def serve_vision_frame(
    printer_id: int,
    filename: str,
    current_user: dict = Depends(require_role("viewer",scope="read")),
    db: Session = Depends(get_db),
):
    return _frame_response(current_user, printer_id, f"{printer_id}/{filename}")


# ============== Vigil AI: Stats ==============

@router.get("/vision/stats", tags=["Vigil AI"])
async def get_vision_stats(
    days: int = Query(7, le=90),
    current_user: dict = Depends(require_role("viewer",scope="read")),
    db: Session = Depends(get_db),
):
    """Legacy records have unknown historical ownership."""
    from modules.vision.training_service import maintenance
    if not maintenance(current_user):
        return {"days":days,"by_type":{},"by_status":{},"by_printer":[],"accuracy_pct":None,"total_reviewed":0}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # By type
    type_rows = (
        db.query(VisionDetection.detection_type, sa_func.count(VisionDetection.id))
        .filter(VisionDetection.created_at > cutoff, _not_training())
        .group_by(VisionDetection.detection_type)
        .all()
    )
    by_type = {dt: cnt for dt, cnt in type_rows}

    # By status
    status_rows = (
        db.query(VisionDetection.status, sa_func.count(VisionDetection.id))
        .filter(VisionDetection.created_at > cutoff, _not_training())
        .group_by(VisionDetection.status)
        .all()
    )
    by_status = {s: cnt for s, cnt in status_rows}

    # By printer (top 10)
    printer_rows = (
        db.query(
            VisionDetection.printer_id,
            Printer.name,
            Printer.nickname,
            sa_func.count(VisionDetection.id).label("count"),
        )
        .outerjoin(Printer, Printer.id == VisionDetection.printer_id)
        .filter(VisionDetection.created_at > cutoff, _not_training())
        .group_by(VisionDetection.printer_id)
        .order_by(sa_func.count(VisionDetection.id).desc())
        .limit(10)
        .all()
    )
    by_printer = [
        {"printer_id": pid, "name": pname, "nickname": pnick, "count": cnt}
        for pid, pname, pnick, cnt in printer_rows
    ]

    # Accuracy (if reviewed detections exist)
    accuracy_row = (
        db.query(
            sa_func.count(VisionDetection.id).label("total_reviewed"),
            sa_func.sum(case((VisionDetection.status == "confirmed", 1), else_=0)).label("confirmed"),
            sa_func.sum(case((VisionDetection.status == "dismissed", 1), else_=0)).label("dismissed"),
        )
        .filter(VisionDetection.created_at > cutoff)
        .filter(VisionDetection.status.in_(["confirmed", "dismissed"]))
        .first()
    )
    total_reviewed = accuracy_row.total_reviewed or 0
    accuracy = None
    if total_reviewed > 0:
        accuracy = round((accuracy_row.confirmed or 0) / total_reviewed * 100, 1)

    return {
        "days": days,
        "by_type": by_type,
        "by_status": by_status,
        "by_printer": by_printer,
        "accuracy_pct": accuracy,
        "total_reviewed": total_reviewed,
    }


# ============== Vigil AI: Training Data ==============

@router.get("/vision/training-data", tags=["Vigil AI"])
async def list_training_data(
    printer_id: Optional[int] = None,
    labeled: Optional[bool] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    current_user: dict = Depends(require_role("admin",scope="read")),
    db: Session = Depends(get_db),
):
    from modules.vision.training_service import list_frames
    return list_frames(db,current_user,printer_id,labeled,limit,offset)


@router.post("/vision/training-data/{detection_id}/label", tags=["Vigil AI"])
async def label_training_data(
    detection_id: int,
    request: Request,
    current_user: dict = Depends(require_role("admin",scope="write")),
    db: Session = Depends(get_db),
):
    from modules.vision.training_service import label_frame
    return label_frame(db,detection_id,await request.json(),current_user)


@router.post("/vision/training-data/{detection_id}/annotation-v2", tags=["Vigil AI"])
async def annotate_training_frame_v2(detection_id: int,request: Request,
    current_user: dict = Depends(require_role("admin",scope="write")),db: Session = Depends(get_db)):
    from modules.vision.training_service import read_annotation_body,versioned_annotation,label_frame
    if type(current_user.get('id')) is not int or current_user['id']<=0 or current_user.get('role')!='admin' or current_user.get('group_id') is not None:
        raise HTTPException(404,'Detection not found')
    body=versioned_annotation(await read_annotation_body(request))
    return label_frame(db,detection_id,body,current_user,expected_digest=body['expected_image_sha256'])


@router.get("/vision/training-data/export", tags=["Vigil AI"])
async def export_training_data(
    current_user: dict = Depends(require_role("admin",scope="read")),
    db: Session = Depends(get_db),
):
    from fastapi.responses import StreamingResponse
    from modules.vision.training_service import export_frames
    return StreamingResponse(export_frames(db,current_user),media_type="application/zip",
        headers={"Content-Disposition":"attachment; filename=odin_training_data.zip"})
