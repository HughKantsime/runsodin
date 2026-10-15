"""O.D.I.N. — Models CRUD, Revisions, and Variants."""

from core.quota import charge_job_quota

from core.rbac import should_filter_org

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import text, bindparam
from typing import List, Optional
from datetime import datetime
import json
import logging
import os
import re
import uuid
from pathlib import Path
from contextlib import contextmanager

from core.db import get_db
from core.db_compat import execute_insert_returning_id
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, _get_org_filter, get_org_scope, check_org_access
from core.models import SystemConfig
from core.model_preview_access import accessible_preview_file, is_education_owned_file, delete_generic_file_if_unchanged
from core.base import FilamentType
from modules.models_library.models import Model
from modules.models_library.schemas import (
    ModelCreate, ModelUpdate, ModelResponse, ModelScheduleOptions,
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
    current_user: dict = Depends(require_role("viewer", scope="read:models")),
    db: Session = Depends(get_db)
):
    """List all print models."""
    query = db.query(Model)
    if category:
        query = query.filter(Model.category == category)

    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if should_filter_org(current_user, effective_org):
        query = query.filter((Model.org_id == effective_org) | (Model.org_id == None))

    return query.order_by(Model.name).all()


@router.get("-with-pricing")
def list_models_with_pricing(
    category: Optional[str] = None,
    org_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer", scope="read:models"))
):
    """List all print models with calculated cost and suggested price."""
    query = db.query(Model)
    if category:
        query = query.filter(Model.category == category)
    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if should_filter_org(current_user, effective_org):
        query = query.filter((Model.org_id == effective_org) | (Model.org_id == None))
    models = query.order_by(Model.name).all()

    # Job aggregates have their own resource permission, independent of models.
    from core.api_token_policy import token_scope_granted
    scopes = current_user.get("_token_scopes", [])
    stats_allowed = not (scopes or current_user.get("_auth_kind") == "user_api_token") or token_scope_granted(scopes, "read:jobs")
    statistics = {model.id: {"completed_job_count": 0, "actual_print_hours": 0.0,
                             "unknown_timing_job_count": 0} for model in models}
    if stats_allowed and models:
        # Persisted Education ownership remains private even without its provider.
        # Include both job payloads and every file linked to the model: legacy
        # Education jobs may have only one of these references.
        statement = """
            SELECT j.model_id,j.actual_start,j.actual_end FROM jobs j
            JOIN models m ON m.id=j.model_id
            WHERE j.status='completed' AND j.model_id IN :stats_model_ids
            AND NOT EXISTS (SELECT 1 FROM education_submissions s
                WHERE s.job_id=j.id OR s.model_id=m.id
                    OR EXISTS (SELECT 1 FROM jobs ej WHERE ej.id=s.job_id AND ej.model_id=m.id)
                    OR s.print_file_id=j.print_file_id OR s.print_file_id=m.print_file_id)
            AND NOT EXISTS (SELECT 1 FROM print_files f
                WHERE (f.id=j.print_file_id OR f.id=m.print_file_id OR f.job_id=j.id OR f.model_id=m.id)
                AND (EXISTS (SELECT 1 FROM education_submissions s
                        JOIN print_files ef ON ef.id=s.print_file_id
                        WHERE s.print_file_id=f.id OR (f.stored_path IS NOT NULL AND ef.stored_path=f.stored_path))
                    OR EXISTS (SELECT 1 FROM education_upload_operations o
                        WHERE f.stored_path IS NOT NULL AND
                            (o.final_path=f.stored_path OR o.staging_path=f.stored_path OR o.cleanup_path=f.stored_path))))
        """
        params = {"stats_model_ids": list(statistics)}
        if should_filter_org(current_user, effective_org):
            if effective_org is None:
                statement += " AND j.charged_to_org_id IS NULL"
            else:
                statement += " AND j.charged_to_org_id=:stats_org"
                params["stats_org"] = effective_org
        for row in db.execute(text(statement).bindparams(bindparam("stats_model_ids", expanding=True)), params):
            stats = statistics.get(row.model_id)
            if stats is None:
                continue  # Only models selected by the authorized library query.
            stats["completed_job_count"] += 1
            try:
                start = datetime.fromisoformat(row.actual_start) if isinstance(row.actual_start, str) else row.actual_start
                end = datetime.fromisoformat(row.actual_end) if isinstance(row.actual_end, str) else row.actual_end
                hours = (end - start).total_seconds() / 3600 if start and end else 0
            except (ValueError, TypeError):
                hours = 0
            if hours > 0:
                stats["actual_print_hours"] += hours
            else:
                stats["unknown_timing_job_count"] += 1

    # Get pricing config once
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG

    # Count and derive dimensions from the same authorized file snapshots.
    variant_counts = {}
    bed_dims = {}
    for model in models:
        variants = _accessible_model_variant_rows(db, model.id, current_user)
        variant_counts[model.id] = len(variants)
        row = next((row for row in variants if row.bed_x_mm is not None), None)
        bed_dims[model.id] = {
            "bed_x_mm": row.bed_x_mm if row else None,
            "bed_y_mm": row.bed_y_mm if row else None,
        }

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
            "variant_count": variant_counts.get(model.id, 0),
            "job_statistics": statistics[model.id] if stats_allowed else None,
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
def create_model(model: ModelCreate, current_user: dict = Depends(require_role("operator", scope="write:models")), db: Session = Depends(get_db)):
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
def get_model(model_id: int, current_user: dict = Depends(require_role("viewer", scope="read:models")), db: Session = Depends(get_db)):
    """Get a specific model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")
    return model


@router.patch("/{model_id}", response_model=ModelResponse)
def update_model(model_id: int, updates: ModelUpdate, current_user: dict = Depends(require_role("operator", scope="write:models")), db: Session = Depends(get_db)):
    """Update a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")

    update_data = updates.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        setattr(model, field, value)

    db.commit()
    db.refresh(model)
    return model


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(model_id: int, current_user: dict = Depends(require_role("operator", scope="write:models")), db: Session = Depends(get_db)):
    """Delete a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")

    db.delete(model)
    db.commit()


def _schedule_file(db, model, principal):
    file_id = model.print_file_id
    if file_id is None:
        file_id = db.execute(text("SELECT id FROM print_files WHERE model_id=:id ORDER BY id DESC LIMIT 1"), {"id": model.id}).scalar()
    if file_id is None:
        return None
    row = accessible_preview_file(db, file_id, principal)
    if row.model_id != model.id:
        raise HTTPException(404, "Print file not found")
    return row


@router.get("/{model_id}/schedule-options")
def model_schedule_options(model_id: int,
    # Viewers read these payload facts to submit a model for approval (org access enforced below).
    current_user: dict = Depends(require_role("viewer", scope="write:jobs")), db: Session = Depends(get_db)):
    from core.interfaces.education_policy import assert_generic_model_reference_allowed
    from core.print_payload_snapshot import hash_regular_payload, stage_payload, PayloadUnavailable
    from core.print_payload import validate_plate
    import zipfile
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model or not check_org_access(current_user, model.org_id):
        raise HTTPException(404, "Model not found")
    assert_generic_model_reference_allowed(db, model.id)
    file = _schedule_file(db, model, current_user)
    if file is None:
        return {"print_file_id": None, "payload_sha256": None, "plate_indices": []}
    indices = []
    try:
        digest = hash_regular_payload(file.stored_path)
        with stage_payload(file.stored_path, digest) as snapshot:
            if Path(file.stored_path).suffix.lower() == ".3mf":
                try:
                    with zipfile.ZipFile(snapshot.path) as archive:
                        indices = sorted({int(match.group(1)) - 1 for name in archive.namelist()
                            if (match := re.fullmatch(r"Metadata/plate_([1-9][0-9]*)\.gcode", name))})
                    for index in indices:
                        validate_plate(snapshot.path, index, "bambu")
                except (OSError, zipfile.BadZipFile):
                    raise HTTPException(400, "Original file is not a readable sliced Bambu 3MF") from None
    except PayloadUnavailable as exc:
        raise HTTPException(400, str(exc)) from None
    return {"print_file_id": file.id, "payload_sha256": digest, "plate_indices": indices}


@router.post("/{model_id}/schedule")
def schedule_from_model(
    model_id: int,
    printer_id: Optional[int] = None,
    options: Optional[ModelScheduleOptions] = None,
    current_user: dict = Depends(require_role("viewer", scope="write:jobs")), db: Session = Depends(get_db)
):
    """Create a print job from a model.

    Operators/admins queue a pending job. Viewers may only submit when the job
    approval workflow is enabled; their job is created 'submitted' and waits
    for an operator/admin to approve it (same rule as POST /jobs).
    """
    from core.interfaces.queue_creation import require_queue_creation_provider
    submission = current_user.get("role") == "viewer"
    if submission:
        approval = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
        if not (approval and approval.value in (True, "true", "True", "1")):
            raise HTTPException(status_code=403, detail="Viewers can only submit jobs for approval when approval is enabled")
        from core.license_manager import require_feature
        require_feature("job_approval")
    queue_creator = require_queue_creation_provider()
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")

    from core.interfaces.education_policy import assert_generic_model_reference_allowed
    from modules.printers.models import Printer
    from core.print_payload_snapshot import hash_regular_payload, stage_payload, PayloadUnavailable
    from core.print_payload import validate_plate
    from core.db_compat import as_utc_datetime

    assert_generic_model_reference_allowed(db, model.id)
    opts = options or ModelScheduleOptions()
    printer = None
    if printer_id is not None:
        printer = db.query(Printer).filter(Printer.id == printer_id).first()
        if not printer or not (check_org_access(current_user, printer.org_id) or printer.shared):
            raise HTTPException(404, "Printer not found")
    file = _schedule_file(db, model, current_user)
    if (opts.print_file_id is None) != (opts.payload_sha256 is None):
        raise HTTPException(400, "File identity and payload hash must be supplied together")
    if opts.print_file_id is not None and (file is None or file.id != opts.print_file_id):
        raise HTTPException(409, "Selected print file changed; refresh scheduling options")
    file_id = payload_hash = plate_index = None
    if file is None:
        if opts.plate_index not in (None, 0):
            raise HTTPException(400, "Plate selection requires a concrete print file")
    else:
        path = file.stored_path
        api_type = printer.api_type if printer else ("bambu" if path and path.lower().endswith(".3mf") else "")
        suffix = Path(path or "").suffix.lower()
        if suffix not in (".3mf", ".gcode", ".bgcode"):
            raise HTTPException(400, "Unsupported print payload format")
        if printer:
            if api_type not in {"bambu", "moonraker", "prusalink"} or not printer.is_active:
                raise HTTPException(400, "Selected printer does not support dispatch")
            allowed = [v.strip() for v in (file.compatible_api_types or "").split(",") if v.strip()]
            if (api_type != "bambu" and suffix == ".3mf") or (allowed and api_type not in allowed):
                raise HTTPException(400, "Print file is incompatible with the selected printer API")
            if all(v is not None for v in (file.bed_x_mm, file.bed_y_mm, printer.bed_x_mm, printer.bed_y_mm)):
                if file.bed_x_mm > printer.bed_x_mm + 2 or file.bed_y_mm > printer.bed_y_mm + 2:
                    raise HTTPException(400, "Print file exceeds the selected printer bed")
        try:
            payload_hash = hash_regular_payload(path)
            if opts.payload_sha256 is not None and opts.payload_sha256 != payload_hash:
                raise HTTPException(409, "Print payload changed; refresh scheduling options")
            with stage_payload(path, payload_hash) as snapshot:
                # A file-bound job must carry the exact plate accepted by dispatch.
                # Preserve the ordinary first-plate default, but validate actual bytes.
                plate_index = opts.plate_index if opts.plate_index is not None else 0
                validate_plate(snapshot.path, plate_index, api_type)
            # Recheck source immediately before admission; snapshot proves validated bytes.
            if hash_regular_payload(path) != payload_hash:
                raise PayloadUnavailable("Print payload changed during scheduling")
        except PayloadUnavailable as exc:
            raise HTTPException(400, str(exc)) from None
        file_id = file.id

    colors = []
    if model.color_requirements:
        req = model.color_requirements if isinstance(model.color_requirements, dict) else json.loads(model.color_requirements)
        for slot_key in sorted(req.keys()):
            slot = req[slot_key]
            if isinstance(slot, dict) and slot.get("color"):
                colors.append(slot["color"])

    # Calculate cost
    estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=model.id)

    charge_job_quota(db, current_user)

    values = {
        "quantity": opts.quantity, "priority": opts.priority,
        "due_date": as_utc_datetime(opts.due_date).replace(tzinfo=None) if opts.due_date else None,
        "notes": opts.notes, "print_file_id": file_id, "payload_sha256": payload_hash, "plate_index": plate_index,
        "item_name": model.name, "model_id": model.id,
        "duration_hours": model.build_time_hours or 0, "colors_required": ','.join(colors),
        "printer_id": printer_id, "estimated_cost": estimated_cost, "suggested_price": suggested_price,
        "charged_to_user_id": current_user.get("id"), "charged_to_org_id": current_user.get("group_id"),
        "submitted_by": current_user.get("id"), "status": "submitted" if submission else "pending",
        "hold": False, "is_locked": False,
    }
    job = queue_creator.stage_values(db, values, current_user)
    job_id = job.id
    alert_plan = None
    if submission:
        from core.base import AlertType, AlertSeverity
        from modules.notifications.alert_dispatcher import (
            stage_alert, get_group_owner_id, get_operator_admin_ids,
        )
        owner_id = get_group_owner_id(db, current_user["id"])
        target_ids = [owner_id] if owner_id else get_operator_admin_ids(db, current_user.get("group_id"))
        alert_plan = stage_alert(db, AlertType.JOB_SUBMITTED, AlertSeverity.INFO,
            f"Job awaiting approval: {model.name or 'Untitled'}",
            f"{current_user.get('display_name') or current_user.get('username', 'A user')} submitted a print job",
            job_id=job_id, target_user_ids=target_ids)
        log_audit(db, "job.created", "job", job_id, {"item_name": model.name, "status": "submitted"})
    db.commit()
    if alert_plan is not None:
        from modules.notifications.alert_dispatcher import deliver_staged_alert
        deliver_staged_alert(db, alert_plan)

    return {
        "job_id": job_id,
        "model_id": model.id,
        "model_name": model.name,
        "status": "submitted" if submission else "pending",
    }


# ──────────────────────────────────────────────
# Model Revisions
# ──────────────────────────────────────────────

@router.get("/{model_id}/revisions")
async def list_model_revisions(model_id: int, current_user: dict = Depends(require_role("viewer", scope="read:models")), db: Session = Depends(get_db)):
    """List all revisions for a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model or not check_org_access(current_user, model.org_id):
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


def _locked_revision_model(db, model_id, current_user):
    # A no-op parent write serializes numbering on SQLite and PostgreSQL.
    db.execute(text("UPDATE models SET id=id WHERE id=:id"), {"id": model_id})
    model = db.query(Model).filter(Model.id == model_id).populate_existing().first()
    if not model or not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")
    return model


def _revision_directory(model_id):
    root = Path(os.environ.get("MODEL_REVISION_STORAGE_ROOT") or "/data/model_revisions")
    if not root.is_absolute():
        raise HTTPException(status_code=400, detail="Revision storage root must be an absolute path")
    root = root.resolve()
    expected = root / str(model_id)
    directory = expected.resolve()
    if directory != expected:
        raise HTTPException(status_code=400, detail="Invalid revision storage path")
    return directory


@contextmanager
def _revision_transaction(db, model_id):
    state = {"new_file": None, "commit_started": False}
    try:
        yield state
        db.flush()
        state["commit_started"] = True
        db.commit()
    except Exception as exc:
        rollback_failed = False
        try:
            db.rollback()
        except Exception:
            rollback_failed = True
        # A commit error can have an uncertain outcome. Preserve the payload then.
        uncertain = state["commit_started"] or rollback_failed
        if state["new_file"] is not None and not uncertain:
            try:
                state["new_file"].unlink(missing_ok=True)
            except OSError:
                log.warning("Revision cleanup failed for model %s", model_id)
        if uncertain:
            log.warning("Revision transaction outcome unknown for model %s", model_id)
            raise HTTPException(status_code=500, detail=f"Revision outcome is uncertain. Contact support with model reference {model_id} before retrying.") from None
        if isinstance(exc, HTTPException):
            raise
        log.warning("Revision write failed for model %s (%s)", model_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Unable to save model revision") from None


def _new_revision_file(model_id, number, filename, state):
    directory = _revision_directory(model_id)
    directory.mkdir(parents=True, exist_ok=True)
    # Revalidate after directory creation; reject a model directory alias outside root.
    if directory != _revision_directory(model_id):
        raise HTTPException(status_code=400, detail="Invalid revision storage path")
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', filename or "file")
    destination = directory / f"v{number}_{uuid.uuid4().hex}_{safe_name}"
    stream = open(destination, "xb")
    state["new_file"] = destination
    return destination, stream


def _insert_revision(db, model_id, number, file_path, changelog, user_id, action):
    db.execute(text("INSERT INTO model_revisions "
                    "(model_id, revision_number, file_path, changelog, uploaded_by) "
                    "VALUES (:mid, :rev, :fp, :cl, :uid)"),
               {"mid": model_id, "rev": number, "fp": file_path, "cl": changelog, "uid": user_id})
    log_audit(db, action, "model", model_id, f"Revision v{number}")


def _next_revision_number(db, model_id):
    return (db.execute(text("SELECT MAX(revision_number) FROM model_revisions WHERE model_id=:mid"),
                       {"mid": model_id}).scalar() or 0) + 1


@router.post("/{model_id}/revisions")
async def create_model_revision(
    model_id: int, changelog: str = "", file: UploadFile = File(None),
    current_user: dict = Depends(require_role("operator", scope="write:models")), db: Session = Depends(get_db)
):
    """Upload a revision, preserving fileless changelog revisions."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model or not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")
    content = None
    if file:
        content = await file.read(100 * 1024 * 1024 + 1)
        if len(content) > 100 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File exceeds 100 MB limit")
    with _revision_transaction(db, model_id) as state:
        _locked_revision_model(db, model_id, current_user)
        next_rev = _next_revision_number(db, model_id)
        file_path = None
        if content is not None:
            destination, stream = _new_revision_file(model_id, next_rev, file.filename, state)
            with stream:
                stream.write(content)
            file_path = str(destination)
        _insert_revision(db, model_id, next_rev, file_path, changelog, current_user["id"], "model_revision_created")
    return {"revision_number": next_rev, "file_path": file_path}


@router.post("/{model_id}/revisions/{rev_number}/revert")
async def revert_model_revision(
    model_id: int, rev_number: int,
    current_user: dict = Depends(require_role("operator", scope="write:models")), db: Session = Depends(get_db)
):
    """Create a new revision from a verified payload or a deliberate fileless revision."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model or not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")
    with _revision_transaction(db, model_id) as state:
        _locked_revision_model(db, model_id, current_user)
        target = db.execute(text("SELECT * FROM model_revisions WHERE model_id=:mid AND revision_number=:rev"),
                            {"mid": model_id, "rev": rev_number}).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail=f"Revision v{rev_number} not found")
        next_rev = _next_revision_number(db, model_id)
        file_path = None
        if target.file_path:
            source = Path(target.file_path).resolve()
            if source.parent != _revision_directory(model_id):
                raise HTTPException(status_code=400, detail="Invalid revision file path")
            if not source.is_file():
                raise HTTPException(status_code=404, detail="Revision file not found")
            destination, stream = _new_revision_file(model_id, next_rev, "reverted" + source.suffix, state)
            import shutil
            with stream:
                with source.open("rb") as original:
                    shutil.copyfileobj(original, stream)
            file_path = str(destination)
        _insert_revision(db, model_id, next_rev, file_path, f"Reverted to v{rev_number}",
                         current_user["id"], "model_revision_reverted")
    return {"revision_number": next_rev, "reverted_from": rev_number}


# ──────────────────────────────────────────────
# Model Variants
# ──────────────────────────────────────────────

def _accessible_model_variant_rows(db, model_id, current_user):
    variants = db.execute(text("""
        SELECT id
        FROM print_files WHERE model_id = :model_id ORDER BY uploaded_at DESC
    """), {"model_id": model_id}).fetchall()

    accessible = []
    for candidate in variants:
        try:
            row = accessible_preview_file(db, candidate.id, current_user)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            continue
        if row.model_id == model_id:
            accessible.append(row)

    return accessible


@router.get("/{model_id}/variants")
def get_model_variants(model_id: int, current_user: dict = Depends(require_role("viewer", scope="read:models")), db: Session = Depends(get_db)):
    """Get all print file variants for a model."""
    model = db.execute(text("SELECT id, name, org_id FROM models WHERE id = :id"), {"id": model_id}).fetchone()
    if not model or not check_org_access(current_user, model[2]):
        raise HTTPException(status_code=404, detail="Model not found")

    accessible = _accessible_model_variant_rows(db, model_id, current_user)

    return {
        "model_id": model_id,
        "model_name": model[1],
        "variants": [{
            "id": v.id, "filename": v.filename, "printer_model": v.printer_model or "Unknown",
            "print_time_seconds": v.print_time_seconds,
            "print_time_hours": round(v.print_time_seconds/3600.0, 2) if v.print_time_seconds else 0,
            "total_weight_grams": v.total_weight_grams, "nozzle_diameter": v.nozzle_diameter,
            "layer_height": v.layer_height, "uploaded_at": v.uploaded_at,
            "bed_x_mm": v.bed_x_mm, "bed_y_mm": v.bed_y_mm,
            "compatible_api_types": v.compatible_api_types, "plate_count": v.plate_count or 1,
        } for v in accessible]
    }


@router.delete("/{model_id}/variants/{variant_id}")
def delete_model_variant(model_id: int, variant_id: int, current_user: dict = Depends(require_role("operator", scope="write:models")), db: Session = Depends(get_db)):
    """Delete a variant from a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model or not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")
    v = db.execute(text("SELECT id, org_id, stored_path, model_id FROM print_files WHERE id=:id AND model_id=:mid"),
                   {"id": variant_id, "mid": model_id}).fetchone()
    if not v or not check_org_access(current_user, v[1]):
        raise HTTPException(status_code=404, detail="Variant not found")
    if is_education_owned_file(db, variant_id, v.stored_path):
        raise HTTPException(status_code=404, detail="Variant not found")

    if not delete_generic_file_if_unchanged(db, v, parent_org_id=model.org_id):
        db.rollback()
        raise HTTPException(status_code=404, detail="Variant not found")
    db.commit()
    remaining = len(get_model_variants(model_id, current_user, db)["variants"])
    return {"message": "Variant deleted", "remaining": remaining}
