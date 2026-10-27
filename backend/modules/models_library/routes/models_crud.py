"""O.D.I.N. — Models CRUD, Revisions, and Variants."""

from core.quota import charge_job_quota

from core.rbac import should_filter_org, require_action

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
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
from core.model_preview_access import accessible_preview_file, is_education_owned_file, delete_generic_file_if_unchanged
from core.base import FilamentType
from modules.models_library.models import Model
from modules.models_library.schemas import (
    ModelCreate, ModelUpdate, ModelResponse, ModelScheduleOptions,
)
from .pricing import calculate_job_cost
from ._models_helpers import (  # noqa: F401
    _locked_revision_model, _new_revision_file, _next_revision_number, _plate_summaries, _revision_directory,
    _require_viewer_submission, _stage_submission_alert,
)

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
    from modules.models_library.education_retention import visible_models
    query = visible_models(db)
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
    from .pricing import list_models_with_pricing as priced_models
    return priced_models(category=category, org_id=org_id, db=db, current_user=current_user,
                         variant_rows=_accessible_model_variant_rows)


@router.post("", response_model=ModelResponse, status_code=status.HTTP_201_CREATED)
def create_model(model: ModelCreate, current_user: dict = Depends(require_action("models.create", scope="write:models")), db: Session = Depends(get_db)):
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
def update_model(model_id: int, updates: ModelUpdate, current_user: dict = Depends(require_action("models.edit", scope="write:models")), db: Session = Depends(get_db)):
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
def delete_model(model_id: int, current_user: dict = Depends(require_action("models.delete", scope="write:models")), db: Session = Depends(get_db)):
    """Delete a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")

    from modules.models_library.education_retention import assert_model_deletable
    assert_model_deletable(db, model.id)
    from modules.orders.services import retain_component_model
    retain_component_model(db,model.org_id or 0,model.id)
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
                # One bounded open of the stored package for every plate.
                from core.print_payload import bambu_plate_numbers
                numbers = bambu_plate_numbers(snapshot.path)
                indices = [number - 1 for number in numbers]
                for index in indices:
                    validate_plate(snapshot.path, index, "bambu", plate_numbers=numbers)
    except PayloadUnavailable as exc:
        raise HTTPException(400, str(exc)) from None
    return {"print_file_id": file.id, "payload_sha256": digest, "plate_indices": indices}


@router.get("/{model_id}/plates")
def model_plate_summaries(model_id: int,
    current_user: dict = Depends(require_action("models.plates", scope="write:jobs")), db: Session = Depends(get_db)):
    """Per-plate sellable pieces, filament grams and print time of the file scheduling would use.

    Model-level fields describe plate 1. Kept apart from schedule-options, whose response is
    echoed back as the schedule body (strict schema).
    """
    from core.print_payload_snapshot import stage_payload, PayloadUnavailable
    options = model_schedule_options(model_id, current_user=current_user, db=db)
    if options["print_file_id"] is None:
        return {**options, "plates": []}
    model = db.query(Model).filter(Model.id == model_id).first()
    file = _schedule_file(db, model, current_user)
    if file is None or file.id != options["print_file_id"]:
        raise HTTPException(409, "Selected print file changed; refresh scheduling options")
    try:
        with stage_payload(file.stored_path, options["payload_sha256"]) as snapshot:
            plates = _plate_summaries(snapshot.path) if Path(file.stored_path).suffix.lower() == ".3mf" else []
    except PayloadUnavailable as exc:
        raise HTTPException(400, str(exc)) from None
    from core.print_file_binding import target_printer
    return {**options, "target_printer_id": target_printer(db, file.id), "plates": plates}


@router.post("/{model_id}/schedule")
def schedule_from_model(
    model_id: int,
    printer_id: Optional[int] = None,
    options: Optional[ModelScheduleOptions] = None,
    current_user: dict = Depends(require_action("jobs.submit", scope="write:jobs")), db: Session = Depends(get_db)
):
    """Create a print job from a model.

    Operators/admins queue a pending job. Viewers may only submit when the job
    approval workflow is enabled; their job is created 'submitted' and waits
    for an operator/admin to approve it (same rule as POST /jobs).
    """
    from core.interfaces.queue_creation import require_queue_creation_provider
    submission = current_user.get("role") == "viewer"
    if submission:
        _require_viewer_submission(db)
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
    from core.ams_mapping import validate_for_printer
    mapping = validate_for_printer(db, opts.ams_mapping, file.filaments_json if file else None, printer_id, current_user)
    from core.print_file_binding import require_bound_target
    require_bound_target(db, file.id if file else None, printer_id)
    if (opts.print_file_id is None) != (opts.payload_sha256 is None):
        raise HTTPException(400, "File identity and payload hash must be supplied together")
    if opts.print_file_id is not None and (file is None or file.id != opts.print_file_id):
        raise HTTPException(409, "Selected print file changed; refresh scheduling options")
    file_id = payload_hash = plate_index = None
    selected_plate = None  # per-plate grams/time when a plate other than plate 1 is scheduled
    other_plate = False    # a 3MF plate other than plate 1 was scheduled
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
                if plate_index > 0 and suffix == ".3mf":
                    other_plate = True
                    selected_plate = next((s for s in _plate_summaries(snapshot.path) if s["plate_index"] == plate_index), None)
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

    # Calculate cost: model-level fields describe plate 1; another plate uses its own grams/time
    # and, when the archive does not record them, the estimate is unknown (never plate 1's).
    plate_grams = selected_plate.get("filament_grams") if selected_plate else None
    plate_seconds = selected_plate.get("print_time_seconds") if selected_plate else None
    plate_hours = round(plate_seconds / 3600.0, 2) if plate_seconds else None
    if not other_plate:
        estimate_basis = "model"
        estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=model.id)
        duration_hours = model.build_time_hours or 0
    elif plate_grams is not None and plate_hours is not None:
        estimate_basis = "selected_plate"
        estimated_cost, suggested_price, _ = calculate_job_cost(
            db, model_id=model.id, filament_grams=plate_grams, print_hours=plate_hours)
        duration_hours = plate_hours
    else:
        estimate_basis = "plate_unknown"
        estimated_cost = suggested_price = None
        duration_hours = plate_hours

    charge_job_quota(db, current_user)

    values = {
        "quantity": opts.quantity, "priority": opts.priority,
        "due_date": as_utc_datetime(opts.due_date).replace(tzinfo=None) if opts.due_date else None,
        "notes": opts.notes, "print_file_id": file_id, "payload_sha256": payload_hash, "plate_index": plate_index,
        "item_name": model.name, "model_id": model.id,
        **({"ams_mapping": mapping} if mapping is not None else {}),
        "duration_hours": duration_hours, "colors_required": ','.join(colors),
        "printer_id": printer_id, "estimated_cost": estimated_cost, "suggested_price": suggested_price,
        # Choosing a printer in the schedule dialog is an explicit choice: pin it.
        **({"target_type": "specific", "target_filter": str(printer_id)} if printer_id is not None else {}),
        "charged_to_user_id": current_user.get("id"), "charged_to_org_id": current_user.get("group_id"),
        "submitted_by": current_user.get("id"), "status": "submitted" if submission else "pending",
        "hold": False, "is_locked": False,
    }
    job = queue_creator.stage_values(db, values, current_user)
    job_id = job.id
    if mapping is not None:
        from core.ams_mapping import mapping_audit_details
        log_audit(db, 'job.ams_mapping_changed', 'job', job.id, mapping_audit_details(job))
    alert_plan = None
    if submission:
        alert_plan = _stage_submission_alert(db, current_user, model, job_id)
        log_audit(db, "job.created", "job", job_id, {"item_name": model.name, "status": "submitted"})
    log_audit(db, "model.scheduled", "model", model.id, {"job_id": job_id, "print_file_id": file_id, "plate_index": plate_index, "printer_id": printer_id})
    db.commit()
    if alert_plan is not None:
        from modules.notifications.alert_dispatcher import deliver_staged_alert
        deliver_staged_alert(db, alert_plan)

    return {
        "job_id": job_id,
        "model_id": model.id,
        "model_name": model.name,
        "status": "submitted" if submission else "pending",
        # model = plate 1 / model-level numbers; selected_plate = that plate's numbers;
        # plate_unknown = the 3MF records no grams/time for that plate (estimate left empty).
        "estimate_basis": estimate_basis,
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


def _insert_revision(db, model_id, number, file_path, changelog, user_id, action):
    db.execute(text("INSERT INTO model_revisions "
                    "(model_id, revision_number, file_path, changelog, uploaded_by) "
                    "VALUES (:mid, :rev, :fp, :cl, :uid)"),
               {"mid": model_id, "rev": number, "fp": file_path, "cl": changelog, "uid": user_id})
    log_audit(db, action, "model", model_id, f"Revision v{number}")


@router.post("/{model_id}/revisions")
async def create_model_revision(
    model_id: int, changelog: str = Query(""), file: UploadFile = File(None),
    changelog_form: Optional[str] = Form(None, alias="changelog"),
    current_user: dict = Depends(require_action("models.create", scope="write:models")), db: Session = Depends(get_db)
):
    """Upload a revision, preserving fileless changelog revisions.

    The changelog is accepted as a multipart form field (what the UI sends) or as the
    legacy query parameter; the form field wins when both are present.
    """
    if changelog_form is not None:
        changelog = changelog_form
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
    current_user: dict = Depends(require_action("models.edit", scope="write:models")), db: Session = Depends(get_db)
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
def delete_model_variant(model_id: int, variant_id: int, current_user: dict = Depends(require_action("models.delete", scope="write:models")), db: Session = Depends(get_db)):
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
