"""O.D.I.N. — Vigil AI: Vision Model Management."""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.orm import Session
from typing import Optional
import logging
import os
import re

from core.db import get_db
from core.rbac import require_role
from modules.vision.models import VisionModel

log = logging.getLogger("odin.api")
router = APIRouter()


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
