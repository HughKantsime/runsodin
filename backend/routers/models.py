"""O.D.I.N. — Model & Print File Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, UploadFile, File
from rate_limit import limiter
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
from datetime import datetime
import json
import logging
import os
import re
import tempfile

from deps import (get_db, get_current_user, require_role, log_audit,
                  _get_org_filter, get_org_scope, check_org_access)
from models import (
    Model, Job, JobStatus, SystemConfig, FilamentLibrary, FilamentType,
)
from schemas import (
    ModelCreate, ModelUpdate, ModelResponse,
)
from config import settings

log = logging.getLogger("odin.api")
logger = log

router = APIRouter()


# ──────────────────────────────────────────────
# Pricing Config
# ──────────────────────────────────────────────

DEFAULT_PRICING_CONFIG = {
    "spool_cost": 25.0,
    "spool_weight": 1000.0,
    "hourly_rate": 15.0,
    "electricity_rate": 0.12,
    "printer_wattage": 100,
    "printer_cost": 300.0,
    "printer_lifespan": 5000,
    "packaging_cost": 0.45,
    "failure_rate": 7.0,
    "monthly_rent": 0.0,
    "parts_per_month": 100,
    "post_processing_min": 5,
    "packing_min": 5,
    "support_min": 5,
    "default_margin": 50.0,
    "other_costs": 0.0,
    "ui_mode": "advanced"
}


# ──────────────────────────────────────────────
# Shared helper (also used by orders)
# ──────────────────────────────────────────────

def calculate_job_cost(db: Session, model_id: int = None, filament_grams: float = 0, print_hours: float = 1.0, material_type: str = "PLA"):
    """Calculate estimated cost and suggested price for a job.

    Returns tuple: (estimated_cost, suggested_price, margin_percent)
    """
    # Get pricing config
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG

    # Get model for defaults if provided
    model = None
    if model_id:
        model = db.query(Model).filter(Model.id == model_id).first()
        if model:
            filament_grams = filament_grams or model.total_filament_grams or 0
            print_hours = print_hours or model.build_time_hours or 1.0
            material_type = model.default_filament_type.value if model.default_filament_type else "PLA"

    # Try to get per-material cost
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

    margin = model.markup_percent if model and model.markup_percent else config["default_margin"]
    suggested_price = subtotal * (1 + margin / 100)

    return (round(subtotal, 2), round(suggested_price, 2), margin)


# ──────────────────────────────────────────────
# Models CRUD
# ──────────────────────────────────────────────

@router.get("/models", response_model=List[ModelResponse], tags=["Models"])
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


@router.get("/models-with-pricing", tags=["Models"])
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


@router.post("/models", response_model=ModelResponse, status_code=status.HTTP_201_CREATED, tags=["Models"])
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


@router.get("/models/{model_id}", response_model=ModelResponse, tags=["Models"])
def get_model(model_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get a specific model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if current_user and not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")
    return model


@router.patch("/models/{model_id}", response_model=ModelResponse, tags=["Models"])
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


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Models"])
def delete_model(model_id: int, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Delete a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")

    db.delete(model)
    db.commit()


@router.post("/models/{model_id}/schedule", tags=["Models"])
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
# Print Files (3MF upload & management)
# ──────────────────────────────────────────────

def _normalize_model_name(name: str) -> str:
    """Strip printer model suffixes for variant matching."""
    # Patterns: " (X1C)", " (H2D)", "_P1S", " - A1", etc.
    patterns = [
        r'\s*[\(\[\-_]\s*(X1C?|X1E|H2D|P1[SP]|A1(\s*Mini)?|Kobra\s*S1)\s*[\)\]]?\s*$',
    ]
    result = name
    for p in patterns:
        result = re.sub(p, '', result, flags=re.IGNORECASE)
    return result.strip()

@router.post("/print-files/upload", tags=["Print Files"])
@limiter.limit("30/minute")
async def upload_3mf(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Upload and parse a print file (.3mf, .gcode, or .bgcode)."""
    import print_file_meta as pfm

    fname = file.filename or ""
    ext = os.path.splitext(fname)[1].lower()

    ALLOWED_EXTENSIONS = {".3mf", ".gcode", ".bgcode"}
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only .3mf, .gcode, and .bgcode files are supported")

    # Enforce upload size limit (100 MB)
    MAX_UPLOAD_BYTES = 100 * 1024 * 1024
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Maximum upload size is 100 MB.")

    # Save to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        if ext == ".3mf":
            # --- .3mf path: full metadata parse ---
            from threemf_parser import parse_3mf, extract_objects_from_plate, extract_mesh_from_3mf

            # Zip bomb check: reject files where uncompressed size exceeds 500 MB
            import zipfile as _zf
            MAX_UNCOMPRESSED = 500 * 1024 * 1024
            try:
                with _zf.ZipFile(tmp_path, 'r') as _z:
                    total_size = sum(e.file_size for e in _z.infolist())
                    if total_size > MAX_UNCOMPRESSED:
                        raise HTTPException(status_code=400, detail="File rejected: decompressed size exceeds 500 MB limit.")
            except _zf.BadZipFile:
                raise HTTPException(status_code=400, detail="Invalid .3mf file (bad zip structure).")

            metadata = parse_3mf(tmp_path)
            if not metadata:
                raise HTTPException(status_code=400, detail="Failed to parse .3mf file")

            # Extract objects for quantity counting
            import zipfile
            with zipfile.ZipFile(tmp_path, 'r') as zf:
                plate_objects = extract_objects_from_plate(zf)

            # Extract 3D mesh for viewer
            mesh_data = extract_mesh_from_3mf(tmp_path)
            mesh_json = json.dumps(mesh_data) if mesh_data else None

            # Store in database
            result = db.execute(text("""
                INSERT INTO print_files (
                    filename, project_name, print_time_seconds, total_weight_grams,
                    layer_count, layer_height, nozzle_diameter, printer_model,
                    supports_used, bed_type, filaments_json, thumbnail_b64, mesh_data
                ) VALUES (
                    :filename, :project_name, :print_time_seconds, :total_weight_grams,
                    :layer_count, :layer_height, :nozzle_diameter, :printer_model,
                    :supports_used, :bed_type, :filaments_json, :thumbnail_b64, :mesh_json
                )
            """), {
                "filename": file.filename,
                "project_name": metadata.project_name,
                "print_time_seconds": metadata.print_time_seconds,
                "total_weight_grams": metadata.total_weight_grams,
                "layer_count": metadata.layer_count,
                "layer_height": metadata.layer_height,
                "nozzle_diameter": metadata.nozzle_diameter,
                "printer_model": metadata.printer_model,
                "supports_used": metadata.supports_used,
                "bed_type": metadata.bed_type,
                "filaments_json": json.dumps([{
                    "slot": f.slot,
                    "type": f.type,
                    "color": f.color,
                    "used_meters": f.used_meters,
                    "used_grams": f.used_grams
                } for f in metadata.filaments]),
                "thumbnail_b64": metadata.thumbnail_b64,
                "mesh_json": mesh_json
            })
            db.commit()

            file_id = result.lastrowid

            # Check for existing model with same name (multi-variant support)
            normalized_name = _normalize_model_name(metadata.project_name)
            existing_model = db.execute(text(
                "SELECT id FROM models WHERE name = :name OR name = :raw_name LIMIT 1"
            ), {"name": normalized_name, "raw_name": metadata.project_name}).fetchone()

            color_req = {}
            for f_item in metadata.filaments:
                color_req[f"slot{f_item.slot}"] = {
                    "color": f_item.color,
                    "grams": round(f_item.used_grams, 2) if f_item.used_grams else 0
                }

            fil_type = "PLA"
            if metadata.filaments:
                fil_type = metadata.filaments[0].type or "PLA"

            if existing_model:
                # Attach as variant to existing model
                model_id = existing_model[0]
                is_new_model = False
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})
                db.commit()
            else:
                # Create new model
                model_result = db.execute(text("""
                    INSERT INTO models (
                        name, build_time_hours, default_filament_type,
                        color_requirements, thumbnail_b64, print_file_id, category
                    ) VALUES (
                        :name, :build_time_hours, :filament_type,
                        :color_requirements, :thumbnail_b64, :print_file_id, :category
                    )
                """), {
                    "name": normalized_name,
                    "build_time_hours": round(metadata.print_time_seconds / 3600.0, 2),
                    "filament_type": fil_type,
                    "color_requirements": json.dumps(color_req),
                    "thumbnail_b64": metadata.thumbnail_b64,
                    "print_file_id": file_id,
                    "category": "Uploaded"
                })
                db.commit()
                model_id = model_result.lastrowid
                is_new_model = True
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})
                db.commit()

            # Persist file to disk
            import shutil
            file_dir = "/data/print_files"
            os.makedirs(file_dir, exist_ok=True)
            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', file.filename)
            stored_path = f"{file_dir}/{file_id}_{safe_name}"
            shutil.copy2(tmp_path, stored_path)
            db.execute(text("UPDATE print_files SET stored_path = :p, original_filename = :fn WHERE id = :id"),
                       {"p": stored_path, "fn": file.filename, "id": file_id})
            db.commit()

            # Extract bed/compatibility metadata and persist
            meta = pfm.extract_print_file_meta(stored_path, ext)
            db.execute(text(
                "UPDATE print_files SET bed_x_mm = :x, bed_y_mm = :y, compatible_api_types = :types WHERE id = :id"
            ), {"x": meta["bed_x_mm"], "y": meta["bed_y_mm"], "types": meta["compatible_api_types"], "id": file_id})
            db.commit()

            return {
                "id": file_id,
                "filename": file.filename,
                "project_name": metadata.project_name,
                "print_time_seconds": metadata.print_time_seconds,
                "print_time_formatted": metadata.print_time_formatted(),
                "total_weight_grams": metadata.total_weight_grams,
                "layer_count": metadata.layer_count,
                "filaments": [{
                    "slot": f.slot,
                    "type": f.type,
                    "color": f.color,
                    "used_grams": f.used_grams
                } for f in metadata.filaments],
                "thumbnail_b64": metadata.thumbnail_b64,
                "is_sliced": metadata.print_time_seconds > 0,
                "model_id": model_id,
                "is_new_model": is_new_model,
                "printer_model": metadata.printer_model,
                "objects": plate_objects,
                "has_mesh": mesh_data is not None,
                "stored_path": stored_path,
                "bed_x_mm": meta["bed_x_mm"],
                "bed_y_mm": meta["bed_y_mm"],
                "compatible_api_types": meta["compatible_api_types"],
            }

        else:
            # --- .gcode / .bgcode path: minimal record, no 3mf parsing ---
            import shutil
            project_name = os.path.splitext(fname)[0]
            normalized_name = _normalize_model_name(project_name)

            result = db.execute(text("""
                INSERT INTO print_files (
                    filename, project_name, filaments_json
                ) VALUES (
                    :filename, :project_name, :filaments_json
                )
            """), {
                "filename": file.filename,
                "project_name": project_name,
                "filaments_json": json.dumps([]),
            })
            db.commit()
            file_id = result.lastrowid

            # Persist file to disk
            file_dir = "/data/print_files"
            os.makedirs(file_dir, exist_ok=True)
            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', file.filename)
            stored_path = f"{file_dir}/{file_id}_{safe_name}"
            shutil.copy2(tmp_path, stored_path)
            db.execute(text("UPDATE print_files SET stored_path = :p, original_filename = :fn WHERE id = :id"),
                       {"p": stored_path, "fn": file.filename, "id": file_id})
            db.commit()

            # Check for existing model or create new
            existing_model = db.execute(text(
                "SELECT id FROM models WHERE name = :name OR name = :raw_name LIMIT 1"
            ), {"name": normalized_name, "raw_name": project_name}).fetchone()

            if existing_model:
                model_id = existing_model[0]
                is_new_model = False
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})
                db.commit()
            else:
                model_result = db.execute(text("""
                    INSERT INTO models (name, default_filament_type, print_file_id, category)
                    VALUES (:name, 'PLA', :print_file_id, 'Uploaded')
                """), {"name": normalized_name, "print_file_id": file_id})
                db.commit()
                model_id = model_result.lastrowid
                is_new_model = True
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})
                db.commit()

            # Extract bed/compatibility metadata and persist
            meta = pfm.extract_print_file_meta(stored_path, ext)
            db.execute(text(
                "UPDATE print_files SET bed_x_mm = :x, bed_y_mm = :y, compatible_api_types = :types WHERE id = :id"
            ), {"x": meta["bed_x_mm"], "y": meta["bed_y_mm"], "types": meta["compatible_api_types"], "id": file_id})
            db.commit()

            return {
                "id": file_id,
                "filename": file.filename,
                "project_name": project_name,
                "print_time_seconds": 0,
                "print_time_formatted": "0m",
                "total_weight_grams": None,
                "layer_count": None,
                "filaments": [],
                "thumbnail_b64": None,
                "is_sliced": True,
                "model_id": model_id,
                "is_new_model": is_new_model,
                "printer_model": None,
                "objects": [],
                "has_mesh": False,
                "stored_path": stored_path,
                "bed_x_mm": meta["bed_x_mm"],
                "bed_y_mm": meta["bed_y_mm"],
                "compatible_api_types": meta["compatible_api_types"],
            }
    finally:
        # Clean up temp file
        os.unlink(tmp_path)


@router.get("/print-files", tags=["Print Files"])
def list_print_files(
    limit: int = Query(default=20, ge=1, le=100),
    include_scheduled: bool = False,
    db: Session = Depends(get_db)
):
    """List uploaded print files."""
    query = """
        SELECT pf.*, j.status as job_status, j.item_name as job_name
        FROM print_files pf
        LEFT JOIN jobs j ON j.id = pf.job_id
    """
    if not include_scheduled:
        query += " WHERE pf.job_id IS NULL"
    query += " ORDER BY pf.uploaded_at DESC LIMIT :limit"

    results = db.execute(text(query), {"limit": limit}).fetchall()

    files = []
    for row in results:
        r = dict(row._mapping)
        r['filaments'] = json.loads(r['filaments_json']) if r['filaments_json'] else []
        del r['filaments_json']
        r['print_time_formatted'] = f"{r['print_time_seconds'] // 3600}h {(r['print_time_seconds'] % 3600) // 60}m" if r['print_time_seconds'] >= 3600 else f"{r['print_time_seconds'] // 60}m"
        files.append(r)

    return files


@router.get("/print-files/{file_id}", tags=["Print Files"])
def get_print_file(file_id: int, db: Session = Depends(get_db)):
    """Get details of a specific print file."""
    result = db.execute(text("SELECT * FROM print_files WHERE id = :id"), {"id": file_id}).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="Print file not found")

    r = dict(result._mapping)
    r['filaments'] = json.loads(r['filaments_json']) if r['filaments_json'] else []
    del r['filaments_json']
    r['print_time_formatted'] = f"{r['print_time_seconds'] // 3600}h {(r['print_time_seconds'] % 3600) // 60}m" if r['print_time_seconds'] >= 3600 else f"{r['print_time_seconds'] // 60}m"

    return r


@router.delete("/print-files/{file_id}", tags=["Print Files"])
def delete_print_file(file_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete an uploaded print file."""
    result = db.execute(text("SELECT id FROM print_files WHERE id = :id"), {"id": file_id}).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="Print file not found")

    db.execute(text("DELETE FROM print_files WHERE id = :id"), {"id": file_id})
    db.commit()
    return {"deleted": True}


@router.post("/print-files/{file_id}/schedule", tags=["Print Files"])
def schedule_print_file(
    file_id: int,
    printer_id: Optional[int] = None,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Create a job from an uploaded print file."""
    # Get the print file
    result = db.execute(text("SELECT * FROM print_files WHERE id = :id"), {"id": file_id}).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="Print file not found")

    pf = dict(result._mapping)
    if pf['job_id']:
        raise HTTPException(status_code=400, detail="File already scheduled")

    filaments = json.loads(pf['filaments_json']) if pf['filaments_json'] else []
    colors = [f['color'] for f in filaments]

    # Create the job
    job_result = db.execute(text("""
        INSERT INTO jobs (
            item_name, duration_hours, colors_required, quantity, priority, status, printer_id, hold, is_locked
        ) VALUES (
            :item_name, :duration_hours, :colors_required, 1, 5, 'pending', :printer_id, 0, 0
        )
    """), {
        "item_name": pf['project_name'],
        "duration_hours": pf['print_time_seconds'] / 3600.0,
        "colors_required": ','.join(colors),
        "printer_id": printer_id
    })
    db.commit()

    job_id = job_result.lastrowid

    # Link the print file to the job
    db.execute(text("UPDATE print_files SET job_id = :job_id WHERE id = :id"), {
        "job_id": job_id,
        "id": file_id
    })
    db.commit()

    return {
        "job_id": job_id,
        "file_id": file_id,
        "project_name": pf['project_name'],
        "status": "pending"
    }


# ──────────────────────────────────────────────
# Model Revisions
# ──────────────────────────────────────────────

@router.get("/models/{model_id}/revisions", tags=["Models"])
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


@router.post("/models/{model_id}/revisions", tags=["Models"])
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


@router.post("/models/{model_id}/revisions/{rev_number}/revert", tags=["Models"])
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
# Mesh / 3D Viewer
# ──────────────────────────────────────────────

@router.get("/print-files/{file_id}/mesh", tags=["3D Viewer"])
async def get_print_file_mesh(file_id: int, db: Session = Depends(get_db)):
    """Get mesh geometry data for 3D viewer from a print file."""
    result = db.execute(text(
        "SELECT mesh_data FROM print_files WHERE id = :id"
    ), {"id": file_id}).fetchone()

    if not result or not result[0]:
        raise HTTPException(status_code=404, detail="No mesh data available for this file")

    import json as json_stdlib
    return json_stdlib.loads(result[0])


@router.get("/models/{model_id}/mesh", tags=["3D Viewer"])
async def get_model_mesh(model_id: int, db: Session = Depends(get_db)):
    """Get mesh geometry for a model (via its linked print_file)."""
    # Find print_file_id from model
    model = db.execute(text(
        "SELECT print_file_id FROM models WHERE id = :id"
    ), {"id": model_id}).fetchone()

    if not model or not model[0]:
        raise HTTPException(status_code=404, detail="Model has no linked print file")

    result = db.execute(text(
        "SELECT mesh_data FROM print_files WHERE id = :id"
    ), {"id": model[0]}).fetchone()

    if not result or not result[0]:
        raise HTTPException(status_code=404, detail="No mesh data available")

    import json as json_stdlib
    return json_stdlib.loads(result[0])


# ──────────────────────────────────────────────
# Model Variants
# ──────────────────────────────────────────────

@router.get("/models/{model_id}/variants", tags=["Models"])
def get_model_variants(model_id: int, db: Session = Depends(get_db)):
    """Get all print file variants for a model."""
    model = db.execute(text("SELECT id, name FROM models WHERE id = :id"), {"id": model_id}).fetchone()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    variants = db.execute(text("""
        SELECT id, filename, printer_model, print_time_seconds, total_weight_grams,
               nozzle_diameter, layer_height, uploaded_at,
               bed_x_mm, bed_y_mm, compatible_api_types
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
        } for v in variants]
    }


@router.delete("/models/{model_id}/variants/{variant_id}", tags=["Models"])
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


# ──────────────────────────────────────────────
# Pricing Config & Model Cost
# ──────────────────────────────────────────────

@router.get("/pricing-config")
def get_pricing_config(db: Session = Depends(get_db)):
    """Get system pricing configuration."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    if not config:
        # Return defaults if not configured
        return DEFAULT_PRICING_CONFIG
    return config.value


@router.put("/pricing-config")
def update_pricing_config(
    config_data: dict,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Update system pricing configuration."""

    # Merge with defaults to ensure all fields exist
    merged_config = {**DEFAULT_PRICING_CONFIG, **config_data}

    config = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    if config:
        config.value = merged_config
    else:
        config = SystemConfig(key="pricing_config", value=merged_config)
        db.add(config)

    db.commit()
    db.refresh(config)

    return config.value


@router.get("/models/{model_id}/cost")
def calculate_model_cost(
    model_id: int,
    db: Session = Depends(get_db)
):
    """Calculate cost breakdown for a model using system pricing config."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    # Get pricing config
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG

    # Calculate costs
    filament_grams = model.total_filament_grams or 0
    print_hours = model.build_time_hours or 1.0

    # Try to get per-material cost from FilamentLibrary
    material_type = model.default_filament_type.value if model.default_filament_type else "PLA"
    filament_entry = db.query(FilamentLibrary).filter(
        FilamentLibrary.material == material_type,
        FilamentLibrary.cost_per_gram.isnot(None)
    ).first()

    if filament_entry and filament_entry.cost_per_gram:
        cost_per_gram = filament_entry.cost_per_gram
        cost_source = f"per-material ({material_type})"
    else:
        cost_per_gram = config["spool_cost"] / config["spool_weight"]
        cost_source = "global default"

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

    return {
        "model_id": model_id,
        "model_name": model.name,
        "filament_grams": filament_grams,
        "print_hours": print_hours,
        "material_type": material_type,
        "cost_per_gram": round(cost_per_gram, 4),
        "cost_source": cost_source,
        "costs": {
            "material": round(material_cost, 2),
            "labor": round(labor_cost, 2),
            "electricity": round(electricity_cost, 2),
            "depreciation": round(depreciation_cost, 2),
            "packaging": round(packaging_cost, 2),
            "failure": round(failure_cost, 2),
            "overhead": round(overhead_cost, 2),
            "other": round(config["other_costs"], 2)
        },
        "subtotal": round(subtotal, 2),
        "margin_percent": margin,
        "suggested_price": round(suggested_price, 2)
    }
