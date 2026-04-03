"""O.D.I.N. — Models CRUD, Revisions, and Variants."""

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
from datetime import datetime
import json
import logging
import os
import re

from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, _get_org_filter, get_org_scope, check_org_access
from core.models import SystemConfig
from core.base import FilamentType
from modules.models_library.models import Model
from modules.models_library.schemas import (
    ModelCreate, ModelUpdate, ModelResponse,
)
from modules.inventory.models import FilamentLibrary
from .pricing import DEFAULT_PRICING_CONFIG, calculate_job_cost

log = logging.getLogger("odin.api")

router = APIRouter(prefix="/models", tags=["Models"])


# ──────────────────────────────────────────────
# Models CRUD
# ──────────────────────────────────────────────

@router.get("", response_model=List[ModelResponse])
def list_models(
    category: Optional[str] = None,
    org_id: Optional[int] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all print models."""
    query = db.query(Model)
    if category:
        query = query.filter(Model.category == category)

    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if effective_org is not None:
        query = query.filter((Model.org_id == effective_org) | (Model.org_id == None))

    return query.order_by(Model.name).all()


@router.get("-with-pricing")
def list_models_with_pricing(
    category: Optional[str] = None,
    org_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
):
    """List all print models with calculated cost and suggested price."""
    query = db.query(Model)
    if category:
        query = query.filter(Model.category == category)
    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if effective_org is not None:
        query = query.filter((Model.org_id == effective_org) | (Model.org_id == None))
    models = query.order_by(Model.name).all()

    # Get pricing config once
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG

    # Get variant counts using raw SQL (print_files has no SQLAlchemy model)
    variant_counts = {}
    for model in models:
        result = db.execute(text("SELECT COUNT(*) FROM print_files WHERE model_id = :mid"), {"mid": model.id}).scalar()
        variant_counts[model.id] = result or 1

    # Get bed dimensions from most recent print file per model
    bed_dims = {}
    for model in models:
        row = db.execute(text(
            "SELECT bed_x_mm, bed_y_mm FROM print_files WHERE model_id = :mid AND bed_x_mm IS NOT NULL ORDER BY uploaded_at DESC LIMIT 1"
        ), {"mid": model.id}).fetchone()
        if row:
            bed_dims[model.id] = {"bed_x_mm": row[0], "bed_y_mm": row[1]}
        else:
            bed_dims[model.id] = {"bed_x_mm": None, "bed_y_mm": None}

    result = []
    for model in models:
        # Calculate cost for this model
        filament_grams = model.total_filament_grams or 0
        print_hours = model.build_time_hours or 1.0

        # Try to get per-material cost
        material_type = model.default_filament_type.value if model.default_filament_type else "PLA"
        filament_entry = db.query(FilamentLibrary).filter(
            FilamentLibrary.material == material_type,
            FilamentLibrary.cost_per_gram.isnot(None)
        ).first()

        if filament_entry and filament_entry.cost_per_gram:
            cost_per_gram = filament_entry.cost_per_gram
        else:
            cost_per_gram = config["spool_cost"] / config["spool_weight"]

        # Calculate costs
        material_cost = filament_grams * cost_per_gram
        labor_hours = (config["post_processing_min"] + config["packing_min"] + config["support_min"]) / 60
        labor_cost = labor_hours * config["hourly_rate"]
        electricity_cost = (config["printer_wattage"] / 1000) * print_hours * config["electricity_rate"]
        depreciation_cost = (config["printer_cost"] / config["printer_lifespan"]) * print_hours
        packaging_cost = config["packaging_cost"]
        base_cost = material_cost + labor_cost + electricity_cost + depreciation_cost + packaging_cost + config["other_costs"]
        failure_cost = base_cost * (config["failure_rate"] / 100)
        overhead_cost = config["monthly_rent"] / config["parts_per_month"] if config["parts_per_month"] > 0 else 0
        subtotal = base_cost + failure_cost + overhead_cost

        margin = model.markup_percent if model.markup_percent else config["default_margin"]
        suggested_price = subtotal * (1 + margin / 100)

        # Build response
        model_dict = {
            "id": model.id,
            "name": model.name,
            "build_time_hours": model.build_time_hours,
            "default_filament_type": model.default_filament_type.value if model.default_filament_type else None,
            "color_requirements": model.color_requirements,
            "category": model.category,
            "thumbnail_url": model.thumbnail_url,
            "thumbnail_b64": model.thumbnail_b64,
            "notes": model.notes,
            "cost_per_item": model.cost_per_item,
            "units_per_bed": model.units_per_bed,
            "markup_percent": model.markup_percent,
            "created_at": model.created_at.isoformat() if model.created_at else None,
            "updated_at": model.updated_at.isoformat() if model.updated_at else None,
            "required_colors": model.required_colors,
            "total_filament_grams": model.total_filament_grams,
            "variant_count": variant_counts.get(model.id, 1),
            # New pricing fields
            "estimated_cost": round(subtotal, 2),
            "suggested_price": round(suggested_price, 2),
            "margin_percent": margin,
            "is_favorite": model.is_favorite or False,
            # Bed dimensions from linked print file (for dispatch compatibility warning)
            "bed_x_mm": bed_dims.get(model.id, {}).get("bed_x_mm"),
            "bed_y_mm": bed_dims.get(model.id, {}).get("bed_y_mm"),
        }
        result.append(model_dict)

    return result


@router.post("", response_model=ModelResponse, status_code=status.HTTP_201_CREATED)
def create_model(model: ModelCreate, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Create a new model definition."""
    # Convert color requirements to dict format
    color_req = None
    if model.color_requirements:
        color_req = {k: v.model_dump() for k, v in model.color_requirements.items()}

    db_model = Model(
        name=model.name,
        build_time_hours=model.build_time_hours,
        default_filament_type=model.default_filament_type,
        color_requirements=color_req,
        category=model.category,
        thumbnail_url=model.thumbnail_url,
        thumbnail_b64=model.thumbnail_b64,
        notes=model.notes,
        cost_per_item=model.cost_per_item,
        units_per_bed=model.units_per_bed,
        quantity_per_bed=model.quantity_per_bed,
        markup_percent=model.markup_percent,
        is_favorite=model.is_favorite,
        org_id=current_user.get("group_id") if current_user else None,
    )
    db.add(db_model)
    db.commit()
    db.refresh(db_model)
    return db_model


@router.get("/{model_id}", response_model=ModelResponse)
def get_model(model_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get a specific model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if current_user and not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")
    return model


@router.patch("/{model_id}", response_model=ModelResponse)
def update_model(model_id: int, updates: ModelUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")

    update_data = updates.model_dump(exclude_unset=True)
    if "color_requirements" in update_data and update_data["color_requirements"]:
        update_data["color_requirements"] = {
            k: v.model_dump() for k, v in update_data["color_requirements"].items()
        }

    for field, value in update_data.items():
        setattr(model, field, value)

    db.commit()
    db.refresh(model)
    return model


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(model_id: int, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Delete a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")

    db.delete(model)
    db.commit()


@router.post("/{model_id}/schedule")
def schedule_from_model(
    model_id: int,
    printer_id: Optional[int] = None,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Create a print job from a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    colors = []
    if model.color_requirements:
        req = model.color_requirements if isinstance(model.color_requirements, dict) else json.loads(model.color_requirements)
        for slot_key in sorted(req.keys()):
            slot = req[slot_key]
            if isinstance(slot, dict) and slot.get("color"):
                colors.append(slot["color"])

    # Calculate cost
    estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=model.id)

    job_result = db.execute(text("""
        INSERT INTO jobs (
            item_name, model_id, duration_hours, colors_required,
            quantity, priority, status, printer_id, hold, is_locked,
            estimated_cost, suggested_price
        ) VALUES (
            :item_name, :model_id, :duration_hours, :colors_required,
            1, 5, 'pending', :printer_id, 0, 0,
            :estimated_cost, :suggested_price
        )
    """), {
        "item_name": model.name,
        "model_id": model.id,
        "duration_hours": model.build_time_hours or 0,
        "colors_required": ','.join(colors),
        "printer_id": printer_id,
        "estimated_cost": estimated_cost,
        "suggested_price": suggested_price
    })
    db.commit()

    return {
        "job_id": job_result.lastrowid,
        "model_id": model.id,
        "model_name": model.name,
        "status": "pending"
    }


# ──────────────────────────────────────────────
# Model Revisions
# ──────────────────────────────────────────────

@router.get("/{model_id}/revisions")
async def list_model_revisions(model_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """List all revisions for a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    rows = db.execute(text(
        "SELECT r.*, u.username as uploaded_by_name FROM model_revisions r "
        "LEFT JOIN users u ON r.uploaded_by = u.id "
        "WHERE r.model_id = :mid ORDER BY r.revision_number DESC"),
        {"mid": model_id}).fetchall()

    return [{
        "id": r.id, "revision_number": r.revision_number,
        "file_path": r.file_path, "changelog": r.changelog,
        "uploaded_by": r.uploaded_by, "uploaded_by_name": r.uploaded_by_name,
        "created_at": r.created_at,
    } for r in rows]


@router.post("/{model_id}/revisions")
async def create_model_revision(
    model_id: int, changelog: str = "", file: UploadFile = File(None),
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Upload a new revision for a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    # Get next revision number
    max_rev = db.execute(text(
        "SELECT MAX(revision_number) FROM model_revisions WHERE model_id = :mid"),
        {"mid": model_id}).scalar() or 0
    next_rev = max_rev + 1

    # Save file if uploaded
    file_path = None
    if file:
        _MAX_REVISION_BYTES = 100 * 1024 * 1024  # 100 MB
        rev_dir = f"/data/model_revisions/{model_id}"
        os.makedirs(rev_dir, exist_ok=True)
        safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', file.filename or "file")
        file_path = f"{rev_dir}/v{next_rev}_{safe_name}"
        with open(file_path, "wb") as f:
            content = await file.read(_MAX_REVISION_BYTES + 1)
            if len(content) > _MAX_REVISION_BYTES:
                raise HTTPException(status_code=413, detail="File exceeds 100 MB limit")
            f.write(content)

    db.execute(text("""INSERT INTO model_revisions (model_id, revision_number, file_path, changelog, uploaded_by)
                       VALUES (:mid, :rev, :fp, :cl, :uid)"""),
               {"mid": model_id, "rev": next_rev, "fp": file_path,
                "cl": changelog, "uid": current_user["id"]})
    db.commit()

    log_audit(db, "model_revision_created", "model", model_id, f"Revision v{next_rev}")
    return {"revision_number": next_rev, "file_path": file_path}


@router.post("/{model_id}/revisions/{rev_number}/revert")
async def revert_model_revision(
    model_id: int, rev_number: int,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Revert a model to a previous revision by creating a new revision from it."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    # Find the target revision
    target = db.execute(text(
        "SELECT * FROM model_revisions WHERE model_id = :mid AND revision_number = :rev"),
        {"mid": model_id, "rev": rev_number}).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail=f"Revision v{rev_number} not found")

    # Get next revision number
    max_rev = db.execute(text(
        "SELECT MAX(revision_number) FROM model_revisions WHERE model_id = :mid"),
        {"mid": model_id}).scalar() or 0
    next_rev = max_rev + 1

    # Copy revision file if it exists
    new_file_path = None
    if target.file_path:
        safe_path = os.path.realpath(target.file_path)
        if not safe_path.startswith('/data/'):
            raise HTTPException(status_code=400, detail="Invalid file path")
        if os.path.exists(safe_path):
            rev_dir = f"/data/model_revisions/{model_id}"
            os.makedirs(rev_dir, exist_ok=True)
            ext = os.path.splitext(target.file_path)[1]
            new_file_path = f"{rev_dir}/v{next_rev}_reverted{ext}"
            import shutil
            shutil.copy2(safe_path, new_file_path)

    db.execute(text("""INSERT INTO model_revisions (model_id, revision_number, file_path, changelog, uploaded_by)
                       VALUES (:mid, :rev, :fp, :cl, :uid)"""),
               {"mid": model_id, "rev": next_rev, "fp": new_file_path,
                "cl": f"Reverted to v{rev_number}", "uid": current_user["id"]})
    db.commit()

    log_audit(db, "model_revision_reverted", "model", model_id, f"Reverted to v{rev_number} as v{next_rev}")
    return {"revision_number": next_rev, "reverted_from": rev_number}


# ──────────────────────────────────────────────
# Model Variants
# ──────────────────────────────────────────────

@router.get("/{model_id}/variants")
def get_model_variants(model_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get all print file variants for a model."""
    model = db.execute(text("SELECT id, name FROM models WHERE id = :id"), {"id": model_id}).fetchone()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    variants = db.execute(text("""
        SELECT id, filename, printer_model, print_time_seconds, total_weight_grams,
               nozzle_diameter, layer_height, uploaded_at,
               bed_x_mm, bed_y_mm, compatible_api_types, plate_count
        FROM print_files WHERE model_id = :model_id ORDER BY uploaded_at DESC
    """), {"model_id": model_id}).fetchall()

    return {
        "model_id": model_id,
        "model_name": model[1],
        "variants": [{
            "id": v[0], "filename": v[1], "printer_model": v[2] or "Unknown",
            "print_time_seconds": v[3], "print_time_hours": round(v[3]/3600.0, 2) if v[3] else 0,
            "total_weight_grams": v[4], "nozzle_diameter": v[5], "layer_height": v[6], "uploaded_at": v[7],
            "bed_x_mm": v[8], "bed_y_mm": v[9], "compatible_api_types": v[10],
            "plate_count": v[11] or 1,
        } for v in variants]
    }


@router.delete("/{model_id}/variants/{variant_id}")
def delete_model_variant(model_id: int, variant_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a variant from a model."""
    v = db.execute(text("SELECT id FROM print_files WHERE id=:id AND model_id=:mid"),
                   {"id": variant_id, "mid": model_id}).fetchone()
    if not v:
        raise HTTPException(status_code=404, detail="Variant not found")

    db.execute(text("DELETE FROM print_files WHERE id = :id"), {"id": variant_id})
    db.commit()
    remaining = db.execute(text("SELECT COUNT(*) FROM print_files WHERE model_id=:mid"),
                           {"mid": model_id}).scalar()
    return {"message": "Variant deleted", "remaining": remaining}
