"""O.D.I.N. — Vigil AI Vision Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func as sa_func, case, text
from typing import Optional
import json
import logging
import os
import re

from deps import get_db, require_role
from models import Printer, SystemConfig, VisionDetection, VisionSettings, VisionModel
from config import settings

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
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """List vision detections with optional filters."""
    query = (
        db.query(VisionDetection, Printer.name, Printer.nickname)
        .outerjoin(Printer, Printer.id == VisionDetection.printer_id)
    )
    count_query = db.query(sa_func.count(VisionDetection.id))

    if printer_id is not None:
        query = query.filter(VisionDetection.printer_id == printer_id)
        count_query = count_query.filter(VisionDetection.printer_id == printer_id)
    if detection_type:
        query = query.filter(VisionDetection.detection_type == detection_type)
        count_query = count_query.filter(VisionDetection.detection_type == detection_type)
    if status_filter:
        query = query.filter(VisionDetection.status == status_filter)
        count_query = count_query.filter(VisionDetection.status == status_filter)

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
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Get a single detection detail."""
    row = (
        db.query(VisionDetection, Printer.name, Printer.nickname)
        .outerjoin(Printer, Printer.id == VisionDetection.printer_id)
        .filter(VisionDetection.id == detection_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Detection not found")
    det, pname, pnick = row
    return _detection_to_dict(det, printer_name=pname, printer_nickname=pnick)


@router.patch("/vision/detections/{detection_id}", tags=["Vigil AI"])
async def review_vision_detection(
    detection_id: int,
    request: Request,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Review a detection: set status to confirmed or dismissed."""
    body = await request.json()
    new_status = body.get("status")
    if new_status not in ("confirmed", "dismissed"):
        raise HTTPException(status_code=400, detail="Status must be 'confirmed' or 'dismissed'")

    det = db.query(VisionDetection).filter(VisionDetection.id == detection_id).first()
    if not det:
        raise HTTPException(status_code=404, detail="Detection not found")

    det.status = new_status
    det.reviewed_by = current_user["id"]
    det.reviewed_at = sa_func.now()
    db.commit()
    return {"id": detection_id, "status": new_status}


# ============== Vigil AI: Per-Printer Vision Settings ==============

@router.get("/printers/{printer_id}/vision", tags=["Vigil AI"])
async def get_printer_vision_settings(
    printer_id: int,
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Get per-printer vision settings."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
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
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Update per-printer vision settings."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
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
        except Exception:
            pass
    return defaults


@router.patch("/vision/settings", tags=["Vigil AI"])
async def update_global_vision_settings(
    request: Request,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Update global vision settings."""
    body = await request.json()
    allowed = {"enabled", "retention_days"}
    updates = {k: v for k, v in body.items() if k in allowed}

    row = db.query(SystemConfig).filter(SystemConfig.key == "vision_settings").first()
    if row:
        existing = json.loads(row.value) if isinstance(row.value, str) else (row.value or {})
        existing.update(updates)
        row.value = existing
    else:
        db.add(SystemConfig(key="vision_settings", value=updates))
    db.commit()
    return updates


# ============== Vigil AI: Frames ==============

@router.get("/vision/frames/{printer_id}/{filename}", tags=["Vigil AI"])
async def serve_vision_frame(
    printer_id: int,
    filename: str,
    current_user: dict = Depends(require_role("viewer")),
):
    """Serve a captured vision frame (path-traversal safe)."""
    from fastapi.responses import FileResponse
    import re as _re

    # Sanitize filename to prevent path traversal
    if not _re.match(r'^[\w\-\.]+$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    frame_path = os.path.join('/data/vision_frames', str(printer_id), filename)
    real_path = os.path.realpath(frame_path)
    if not real_path.startswith('/data/vision_frames/'):
        raise HTTPException(status_code=404, detail="Not found")

    if not os.path.isfile(real_path):
        raise HTTPException(status_code=404, detail="Frame not found")

    return FileResponse(real_path, media_type="image/jpeg")


# ============== Vigil AI: Models ==============

@router.get("/vision/models", tags=["Vigil AI"])
async def list_vision_models(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """List registered ONNX models."""
    rows = db.query(VisionModel).order_by(VisionModel.uploaded_at.desc()).all()
    return [
        {
            "id": m.id,
            "name": m.name,
            "detection_type": m.detection_type,
            "filename": m.filename,
            "version": m.version,
            "input_size": m.input_size,
            "is_active": m.is_active,
            "metadata_json": m.metadata_json,
            "uploaded_at": m.uploaded_at.isoformat() if m.uploaded_at else None,
        }
        for m in rows
    ]


@router.post("/vision/models", tags=["Vigil AI"])
async def upload_vision_model(
    file: UploadFile = File(...),
    name: str = Query(...),
    detection_type: str = Query(...),
    version: Optional[str] = None,
    input_size: int = Query(640),
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Upload a custom ONNX model."""
    if detection_type not in ('spaghetti', 'first_layer', 'detachment', 'build_plate_empty'):
        raise HTTPException(status_code=400, detail="Invalid detection_type")

    if not file.filename.endswith('.onnx'):
        raise HTTPException(status_code=400, detail="File must be .onnx")

    # Save file
    MAX_ONNX_BYTES = 500 * 1024 * 1024  # 500 MB — ONNX models can be large
    content = await file.read(MAX_ONNX_BYTES + 1)
    if len(content) > MAX_ONNX_BYTES:
        raise HTTPException(status_code=413, detail="Model file exceeds 500 MB limit")
    os.makedirs('/data/vision_models', exist_ok=True)
    safe_name = re.sub(r'[^\w\-\.]', '_', file.filename)
    dest = os.path.join('/data/vision_models', safe_name)
    with open(dest, 'wb') as f:
        f.write(content)

    # Register in DB
    model = VisionModel(
        name=name,
        detection_type=detection_type,
        filename=safe_name,
        version=version,
        input_size=input_size,
    )
    db.add(model)
    db.commit()
    db.refresh(model)

    return {"id": model.id, "name": name, "filename": safe_name}


@router.patch("/vision/models/{model_id}/activate", tags=["Vigil AI"])
async def activate_vision_model(
    model_id: int,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Set a model as active for its detection type (deactivates others of same type)."""
    model = db.query(VisionModel).filter(VisionModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    dt = model.detection_type
    # Deactivate all models of same type
    db.query(VisionModel).filter(VisionModel.detection_type == dt).update({"is_active": 0})
    # Activate this one
    model.is_active = 1
    db.commit()

    return {"id": model_id, "detection_type": dt, "is_active": True}


# ============== Vigil AI: Stats ==============

@router.get("/vision/stats", tags=["Vigil AI"])
async def get_vision_stats(
    days: int = Query(7, le=90),
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Detection statistics: counts by type, status, and printer."""
    cutoff = sa_func.datetime("now", f"-{days} days")

    # By type
    type_rows = (
        db.query(VisionDetection.detection_type, sa_func.count(VisionDetection.id))
        .filter(VisionDetection.created_at > cutoff)
        .group_by(VisionDetection.detection_type)
        .all()
    )
    by_type = {dt: cnt for dt, cnt in type_rows}

    # By status
    status_rows = (
        db.query(VisionDetection.status, sa_func.count(VisionDetection.id))
        .filter(VisionDetection.created_at > cutoff)
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
        .filter(VisionDetection.created_at > cutoff)
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
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """List captured training frames with labels."""
    query = db.query(
        VisionDetection.id,
        VisionDetection.printer_id,
        VisionDetection.detection_type,
        VisionDetection.confidence,
        VisionDetection.status,
        VisionDetection.frame_path,
        VisionDetection.bbox_json,
        VisionDetection.created_at,
    ).filter(VisionDetection.frame_path.isnot(None))

    if printer_id is not None:
        query = query.filter(VisionDetection.printer_id == printer_id)
    if labeled is True:
        query = query.filter(VisionDetection.status.in_(["confirmed", "dismissed"]))
    elif labeled is False:
        query = query.filter(VisionDetection.status == "pending")

    rows = (
        query.order_by(VisionDetection.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [
        {
            "id": r.id,
            "printer_id": r.printer_id,
            "detection_type": r.detection_type,
            "confidence": r.confidence,
            "status": r.status,
            "frame_path": r.frame_path,
            "bbox_json": r.bbox_json,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/vision/training-data/{detection_id}/label", tags=["Vigil AI"])
async def label_training_data(
    detection_id: int,
    request: Request,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Save a label (class + bbox) for a training frame."""
    body = await request.json()
    label_class = body.get("class")  # detection_type
    bbox = body.get("bbox")  # [x1, y1, x2, y2]

    if not label_class or bbox is None:
        raise HTTPException(status_code=400, detail="class and bbox are required")

    VALID_DETECTION_TYPES = {"spaghetti", "first_layer_failure", "detachment", "false_positive"}
    if label_class not in VALID_DETECTION_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid detection type. Must be one of: {', '.join(sorted(VALID_DETECTION_TYPES))}")

    det = db.query(VisionDetection).filter(VisionDetection.id == detection_id).first()
    if not det:
        raise HTTPException(status_code=404, detail="Detection not found")

    metadata = json.dumps({"label_class": label_class, "label_bbox": bbox})
    det.metadata_json = metadata
    det.detection_type = label_class
    db.commit()
    return {"id": detection_id, "labeled": True}


@router.get("/vision/training-data/export", tags=["Vigil AI"])
async def export_training_data(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Download labeled dataset as ZIP in YOLO format."""
    import zipfile
    import io
    from fastapi.responses import StreamingResponse

    rows = (
        db.query(
            VisionDetection.id,
            VisionDetection.detection_type,
            VisionDetection.frame_path,
            VisionDetection.bbox_json,
            VisionDetection.metadata_json,
        )
        .filter(VisionDetection.status.in_(["confirmed", "dismissed"]))
        .filter(VisionDetection.frame_path.isnot(None))
        .all()
    )

    # Class mapping
    class_map = {'spaghetti': 0, 'first_layer': 1, 'detachment': 2}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Write class names
        zf.writestr('data.yaml', (
            "names:\n"
            "  0: spaghetti\n"
            "  1: first_layer\n"
            "  2: detachment\n"
            f"nc: 3\n"
            "train: images/\n"
            "val: images/\n"
        ))

        for row in rows:
            frame_abs = os.path.realpath(os.path.join('/data/vision_frames', row.frame_path))
            if not frame_abs.startswith('/data/vision_frames/'):
                continue  # skip corrupted or injected entries
            if not os.path.isfile(frame_abs):
                continue

            base = os.path.splitext(os.path.basename(row.frame_path))[0]
            zf.write(frame_abs, f"images/{os.path.basename(row.frame_path)}")

            # YOLO label format: class_id cx cy w h (normalized)
            bbox = json.loads(row.bbox_json) if row.bbox_json else None
            dt = row.detection_type
            class_id = class_map.get(dt, 0)

            if bbox and len(bbox) == 4:
                # Read image dims for normalization
                import cv2
                img = cv2.imread(frame_abs)
                if img is not None:
                    ih, iw = img.shape[:2]
                    x1, y1, x2, y2 = bbox
                    cx = ((x1 + x2) / 2) / iw
                    cy = ((y1 + y2) / 2) / ih
                    w = (x2 - x1) / iw
                    h = (y2 - y1) / ih
                    zf.writestr(f"labels/{base}.txt", f"{class_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=odin_training_data.zip"}
    )
