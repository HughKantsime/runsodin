"""O.D.I.N. — Vigil AI Vision Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from sqlalchemy.orm import Session
from typing import Optional
import json
import logging
import os
import re
import sqlite3 as _sqlite3

from deps import get_db, require_role
from models import Printer, SystemConfig
from config import settings

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Vigil AI: Detections ==============

@router.get("/api/vision/detections", tags=["Vigil AI"])
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
    conn = _sqlite3.connect(db.bind.url.database if hasattr(db.bind.url, 'database') else '/data/odin.db')
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()

    where = []
    params = []
    if printer_id is not None:
        where.append("vd.printer_id = ?")
        params.append(printer_id)
    if detection_type:
        where.append("vd.detection_type = ?")
        params.append(detection_type)
    if status_filter:
        where.append("vd.status = ?")
        params.append(status_filter)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params_count = list(params)
    params += [limit, offset]

    cur.execute(f"""
        SELECT vd.*, p.name as printer_name, p.nickname as printer_nickname
        FROM vision_detections vd
        LEFT JOIN printers p ON p.id = vd.printer_id
        {where_sql}
        ORDER BY vd.created_at DESC
        LIMIT ? OFFSET ?
    """, params)
    rows = [dict(r) for r in cur.fetchall()]

    cur.execute(f"SELECT COUNT(*) FROM vision_detections vd {where_sql}", params_count)
    total = cur.fetchone()[0]
    conn.close()

    return {"items": rows, "total": total}


@router.get("/api/vision/detections/{detection_id}", tags=["Vigil AI"])
async def get_vision_detection(
    detection_id: int,
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Get a single detection detail."""
    conn = _sqlite3.connect('/data/odin.db')
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT vd.*, p.name as printer_name, p.nickname as printer_nickname
        FROM vision_detections vd
        LEFT JOIN printers p ON p.id = vd.printer_id
        WHERE vd.id = ?
    """, (detection_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Detection not found")
    return dict(row)


@router.patch("/api/vision/detections/{detection_id}", tags=["Vigil AI"])
async def review_vision_detection(
    detection_id: int,
    request: Request,
    current_user: dict = Depends(require_role("operator")),
):
    """Review a detection: set status to confirmed or dismissed."""
    body = await request.json()
    new_status = body.get("status")
    if new_status not in ("confirmed", "dismissed"):
        raise HTTPException(status_code=400, detail="Status must be 'confirmed' or 'dismissed'")

    conn = _sqlite3.connect('/data/odin.db')
    cur = conn.cursor()
    cur.execute("SELECT id FROM vision_detections WHERE id = ?", (detection_id,))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Detection not found")

    cur.execute(
        """UPDATE vision_detections
        SET status = ?, reviewed_by = ?, reviewed_at = datetime('now')
        WHERE id = ?""",
        (new_status, current_user["id"], detection_id)
    )
    conn.commit()
    conn.close()
    return {"id": detection_id, "status": new_status}


# ============== Vigil AI: Per-Printer Vision Settings ==============

@router.get("/api/printers/{printer_id}/vision", tags=["Vigil AI"])
async def get_printer_vision_settings(
    printer_id: int,
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Get per-printer vision settings."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    conn = _sqlite3.connect('/data/odin.db')
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM vision_settings WHERE printer_id = ?", (printer_id,))
    row = cur.fetchone()
    conn.close()

    if row:
        return dict(row)
    # Return defaults
    return {
        "printer_id": printer_id,
        "enabled": 1,
        "spaghetti_enabled": 1, "spaghetti_threshold": 0.65,
        "first_layer_enabled": 1, "first_layer_threshold": 0.60,
        "detachment_enabled": 1, "detachment_threshold": 0.70,
        "auto_pause": 0, "capture_interval_sec": 10,
        "collect_training_data": 0,
    }


@router.patch("/api/printers/{printer_id}/vision", tags=["Vigil AI"])
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
        "auto_pause", "capture_interval_sec", "collect_training_data",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    conn = _sqlite3.connect('/data/odin.db')
    cur = conn.cursor()

    # Upsert
    cur.execute("SELECT printer_id FROM vision_settings WHERE printer_id = ?", (printer_id,))
    if cur.fetchone():
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [printer_id]
        cur.execute(f"UPDATE vision_settings SET {set_clause}, updated_at = datetime('now') WHERE printer_id = ?", params)
    else:
        updates["printer_id"] = printer_id
        cols = ", ".join(updates.keys())
        placeholders = ", ".join("?" for _ in updates)
        cur.execute(f"INSERT INTO vision_settings ({cols}) VALUES ({placeholders})", list(updates.values()))

    conn.commit()
    conn.close()
    return {"printer_id": printer_id, **updates}


# ============== Vigil AI: Global Vision Settings ==============

@router.get("/api/vision/settings", tags=["Vigil AI"])
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


@router.patch("/api/vision/settings", tags=["Vigil AI"])
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

@router.get("/api/vision/frames/{printer_id}/{filename}", tags=["Vigil AI"])
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

@router.get("/api/vision/models", tags=["Vigil AI"])
async def list_vision_models(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """List registered ONNX models."""
    conn = _sqlite3.connect('/data/odin.db')
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM vision_models ORDER BY uploaded_at DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@router.post("/api/vision/models", tags=["Vigil AI"])
async def upload_vision_model(
    file: UploadFile = File(...),
    name: str = Query(...),
    detection_type: str = Query(...),
    version: Optional[str] = None,
    input_size: int = Query(640),
    current_user: dict = Depends(require_role("admin")),
):
    """Upload a custom ONNX model."""
    if detection_type not in ('spaghetti', 'first_layer', 'detachment'):
        raise HTTPException(status_code=400, detail="Invalid detection_type")

    if not file.filename.endswith('.onnx'):
        raise HTTPException(status_code=400, detail="File must be .onnx")

    # Save file
    os.makedirs('/data/vision_models', exist_ok=True)
    safe_name = re.sub(r'[^\w\-\.]', '_', file.filename)
    dest = os.path.join('/data/vision_models', safe_name)
    with open(dest, 'wb') as f:
        content = await file.read()
        f.write(content)

    # Register in DB
    conn = _sqlite3.connect('/data/odin.db')
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO vision_models (name, detection_type, filename, version, input_size, uploaded_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (name, detection_type, safe_name, version, input_size)
    )
    model_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {"id": model_id, "name": name, "filename": safe_name}


@router.patch("/api/vision/models/{model_id}/activate", tags=["Vigil AI"])
async def activate_vision_model(
    model_id: int,
    current_user: dict = Depends(require_role("admin")),
):
    """Set a model as active for its detection type (deactivates others of same type)."""
    conn = _sqlite3.connect('/data/odin.db')
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT id, detection_type FROM vision_models WHERE id = ?", (model_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Model not found")

    dt = row['detection_type']
    # Deactivate all models of same type
    cur.execute("UPDATE vision_models SET is_active = 0 WHERE detection_type = ?", (dt,))
    # Activate this one
    cur.execute("UPDATE vision_models SET is_active = 1 WHERE id = ?", (model_id,))
    conn.commit()
    conn.close()

    return {"id": model_id, "detection_type": dt, "is_active": True}


# ============== Vigil AI: Stats ==============

@router.get("/api/vision/stats", tags=["Vigil AI"])
async def get_vision_stats(
    days: int = Query(7, le=90),
    current_user: dict = Depends(require_role("viewer")),
):
    """Detection statistics: counts by type, status, and printer."""
    conn = _sqlite3.connect('/data/odin.db')
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()

    cutoff = f"-{days} days"

    # By type
    cur.execute("""
        SELECT detection_type, COUNT(*) as count
        FROM vision_detections
        WHERE created_at > datetime('now', ?)
        GROUP BY detection_type
    """, (cutoff,))
    by_type = {r['detection_type']: r['count'] for r in cur.fetchall()}

    # By status
    cur.execute("""
        SELECT status, COUNT(*) as count
        FROM vision_detections
        WHERE created_at > datetime('now', ?)
        GROUP BY status
    """, (cutoff,))
    by_status = {r['status']: r['count'] for r in cur.fetchall()}

    # By printer (top 10)
    cur.execute("""
        SELECT vd.printer_id, p.name, p.nickname, COUNT(*) as count
        FROM vision_detections vd
        LEFT JOIN printers p ON p.id = vd.printer_id
        WHERE vd.created_at > datetime('now', ?)
        GROUP BY vd.printer_id
        ORDER BY count DESC LIMIT 10
    """, (cutoff,))
    by_printer = [dict(r) for r in cur.fetchall()]

    # Accuracy (if reviewed detections exist)
    cur.execute("""
        SELECT
            COUNT(*) as total_reviewed,
            SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) as confirmed,
            SUM(CASE WHEN status = 'dismissed' THEN 1 ELSE 0 END) as dismissed
        FROM vision_detections
        WHERE created_at > datetime('now', ?)
          AND status IN ('confirmed', 'dismissed')
    """, (cutoff,))
    accuracy_row = cur.fetchone()
    total_reviewed = accuracy_row['total_reviewed'] or 0
    accuracy = None
    if total_reviewed > 0:
        accuracy = round((accuracy_row['confirmed'] or 0) / total_reviewed * 100, 1)

    conn.close()

    return {
        "days": days,
        "by_type": by_type,
        "by_status": by_status,
        "by_printer": by_printer,
        "accuracy_pct": accuracy,
        "total_reviewed": total_reviewed,
    }


# ============== Vigil AI: Training Data ==============

@router.get("/api/vision/training-data", tags=["Vigil AI"])
async def list_training_data(
    printer_id: Optional[int] = None,
    labeled: Optional[bool] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    current_user: dict = Depends(require_role("admin")),
):
    """List captured training frames with labels."""
    conn = _sqlite3.connect('/data/odin.db')
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()

    # Training data = confirmed/dismissed detections (labeled) + frames from collect mode
    where = ["1=1"]
    params = []
    if printer_id is not None:
        where.append("vd.printer_id = ?")
        params.append(printer_id)
    if labeled is True:
        where.append("vd.status IN ('confirmed', 'dismissed')")
    elif labeled is False:
        where.append("vd.status = 'pending'")

    where_sql = " AND ".join(where)
    params_extra = list(params) + [limit, offset]

    cur.execute(f"""
        SELECT vd.id, vd.printer_id, vd.detection_type, vd.confidence,
               vd.status, vd.frame_path, vd.bbox_json, vd.created_at
        FROM vision_detections vd
        WHERE {where_sql} AND vd.frame_path IS NOT NULL
        ORDER BY vd.created_at DESC
        LIMIT ? OFFSET ?
    """, params_extra)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@router.post("/api/vision/training-data/{detection_id}/label", tags=["Vigil AI"])
async def label_training_data(
    detection_id: int,
    request: Request,
    current_user: dict = Depends(require_role("admin")),
):
    """Save a label (class + bbox) for a training frame."""
    body = await request.json()
    label_class = body.get("class")  # detection_type
    bbox = body.get("bbox")  # [x1, y1, x2, y2]

    if not label_class or bbox is None:
        raise HTTPException(status_code=400, detail="class and bbox are required")

    conn = _sqlite3.connect('/data/odin.db')
    cur = conn.cursor()
    cur.execute("SELECT id FROM vision_detections WHERE id = ?", (detection_id,))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Detection not found")

    metadata = json.dumps({"label_class": label_class, "label_bbox": bbox})
    cur.execute(
        "UPDATE vision_detections SET metadata_json = ?, detection_type = ? WHERE id = ?",
        (metadata, label_class, detection_id)
    )
    conn.commit()
    conn.close()
    return {"id": detection_id, "labeled": True}


@router.get("/api/vision/training-data/export", tags=["Vigil AI"])
async def export_training_data(
    current_user: dict = Depends(require_role("admin")),
):
    """Download labeled dataset as ZIP in YOLO format."""
    import zipfile
    import io
    from fastapi.responses import StreamingResponse

    conn = _sqlite3.connect('/data/odin.db')
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT id, detection_type, frame_path, bbox_json, metadata_json
        FROM vision_detections
        WHERE status IN ('confirmed', 'dismissed')
          AND frame_path IS NOT NULL
    """)
    rows = cur.fetchall()
    conn.close()

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
            frame_abs = os.path.join('/data/vision_frames', row['frame_path'])
            if not os.path.isfile(frame_abs):
                continue

            base = os.path.splitext(os.path.basename(row['frame_path']))[0]
            zf.write(frame_abs, f"images/{os.path.basename(row['frame_path'])}")

            # YOLO label format: class_id cx cy w h (normalized)
            bbox = json.loads(row['bbox_json']) if row['bbox_json'] else None
            dt = row['detection_type']
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
