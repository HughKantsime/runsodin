"""O.D.I.N. — Vigil AI: Vision Model Management."""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import delete
from typing import Optional
import logging
import os
import uuid
import hashlib
from modules.vision.runtime_status import read_status, reported_readiness, registry_fingerprint
import json
from pathlib import Path

from core.db import get_db
from core.rbac import require_superadmin
from modules.vision.models import VisionModel

log = logging.getLogger("odin.api")
router = APIRouter()
VISION_MODELS_DIR = "/data/vision_models"


@router.get("/vision/models", tags=["Vigil AI"])
async def list_vision_models(
    current_user: dict = Depends(require_superadmin(scope="read")),
    db: Session = Depends(get_db),
):
    """List registered ONNX models."""
    rows = db.query(VisionModel).order_by(VisionModel.uploaded_at.desc()).all()
    report = read_status(str(Path(VISION_MODELS_DIR) / '.runtime'))
    def readiness(model):
        try:
            path = Path(VISION_MODELS_DIR) / model.filename
            digest = hashlib.sha256()
            with path.open('rb') as source:
                total = 0
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    total += len(chunk)
                    if total > 500 * 1024 * 1024:
                        return {'state':'unknown','reported_at':None}
                    digest.update(chunk)
            fingerprint = registry_fingerprint(model.id,model.filename,model.input_size,
                model.metadata_json,digest.hexdigest())
            return reported_readiness(report,model.detection_type,model.id,fingerprint)
        except (OSError,ValueError):
            return {'state':'unknown','reported_at':None}
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
            "runtime_status": readiness(m) if m.is_active else {"state":"inactive","reported_at":None},
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
    input_size: int = Query(640, ge=16, le=4096),
    inference_contract: Optional[str] = Query(None, max_length=16384),
    current_user: dict = Depends(require_superadmin(scope="write")),
    db: Session = Depends(get_db),
):
    """Upload a custom ONNX model."""
    if detection_type not in ('spaghetti', 'first_layer', 'detachment', 'build_plate_empty'):
        raise HTTPException(status_code=400, detail="Invalid detection_type")

    if not file.filename.endswith('.onnx'):
        raise HTTPException(status_code=400, detail="File must be .onnx")

    metadata = None
    if inference_contract is not None:
        from modules.vision.output_contract import OutputContract, InvalidModelOutput
        try:
            value = json.loads(inference_contract)
            OutputContract.from_metadata({'inference_contract': value})
            metadata = json.dumps({'inference_contract': value})
        except (ValueError, TypeError, RecursionError, InvalidModelOutput):
            raise HTTPException(status_code=400, detail="Invalid inference contract") from None

    # Every registry upload owns a unique immutable file. Client names must
    # not replace bytes used by an existing row or in-flight session.
    MAX_ONNX_BYTES = 500 * 1024 * 1024  # 500 MB — ONNX models can be large
    content = await file.read(MAX_ONNX_BYTES + 1)
    if len(content) > MAX_ONNX_BYTES:
        raise HTTPException(status_code=413, detail="Model file exceeds 500 MB limit")
    os.makedirs('/data/vision_models', exist_ok=True)
    safe_name = uuid.uuid4().hex + '.onnx'
    dest = os.path.join('/data/vision_models', safe_name)
    with open(dest, 'xb') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    directory_fd = os.open(str(Path(dest).parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

    # Register in DB
    model = VisionModel(
        name=name,
        detection_type=detection_type,
        filename=safe_name,
        version=version,
        input_size=input_size,
        metadata_json=metadata,
    )
    try:
        db.add(model)
        db.commit()
        db.refresh(model)
    except Exception:
        # A failed acknowledgement may follow a committed row. Preserve this
        # owned immutable file rather than destroy potentially published bytes.
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="Model registration unconfirmed") from None

    return {"id": model.id, "name": name, "filename": safe_name}


@router.patch("/vision/models/{model_id}/activate", tags=["Vigil AI"])
async def activate_vision_model(
    model_id: int,
    current_user: dict = Depends(require_superadmin(scope="write")),
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
    model.is_active = True
    db.commit()

    return {"id": model_id, "detection_type": dt, "is_active": True}


@router.delete("/vision/models/{model_id}", tags=["Vigil AI"])
async def delete_vision_model(
    model_id: int,
    current_user: dict = Depends(require_superadmin(scope="write")),
    db: Session = Depends(get_db),
):
    """Retire an inactive registry entry, retaining immutable bytes for controlled cleanup."""
    model = db.query(VisionModel).filter(VisionModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    if model.is_active:
        raise HTTPException(status_code=400, detail="Cannot delete an active model. Deactivate it first.")

    # Logical retirement is conditional at the write boundary. Immutable
    # model bytes are retained; removal requires a separately controlled
    # cleanup that accounts for live sessions and legacy shared filenames.
    try:
        result = db.execute(delete(VisionModel).where(
            VisionModel.id == model_id, VisionModel.is_active.is_not(True)
        ))
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="Model state changed")
        db.commit()
    except HTTPException:
        raise
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="Model retirement unconfirmed") from None
    return {"id": model_id, "deleted": True}
