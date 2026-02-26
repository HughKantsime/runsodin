"""Job CRUD, creation, bulk operations, and queue management."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel as PydanticBaseModel
import json
import logging

from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, _get_org_filter, get_org_scope, check_org_access
from core.quota import _get_period_key, _get_quota_usage
from core.base import JobStatus, AlertType, AlertSeverity
from modules.jobs.models import Job
from modules.printers.models import Printer
from modules.jobs.schemas import (
    JobCreate, JobUpdate, JobResponse, JobSummary,
)
from core.models import SystemConfig

log = logging.getLogger("odin.api")
logger = log

router = APIRouter(prefix="/jobs", tags=["Jobs"])


class JobBatchRequest(PydanticBaseModel):
    """Send the same job to multiple printers simultaneously."""
    item_name: str
    model_id: Optional[int] = None
    printer_ids: List[int]
    priority: int = 3
    duration_hours: Optional[float] = None
    colors_required: Optional[str] = None
    filament_type: Optional[str] = None
    notes: Optional[str] = None
    queue_only: bool = False


class JobReorderRequest(PydanticBaseModel):
    job_ids: list[int]


@router.get("/filament-check", tags=["Jobs"])
def check_filament_compatibility(
    printer_id: int,
    filament_type: Optional[str] = None,
    colors: Optional[str] = None,
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Check if a printer has compatible filament loaded. Advisory only — never blocks job creation."""
    from modules.printers.models import FilamentSlot
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    warnings = []
    slots = db.query(FilamentSlot).filter(FilamentSlot.printer_id == printer_id).all()

    if filament_type and slots:
        loaded_types = {s.filament_type.value.upper() if s.filament_type else '' for s in slots}
        if filament_type.upper() not in loaded_types and 'EMPTY' not in loaded_types:
            warnings.append(f"Job requires {filament_type} but printer has {', '.join(t for t in loaded_types if t)} loaded")

    if colors and slots:
        required = [c.strip().lower() for c in colors.split(',') if c.strip()]
        loaded_colors = {(s.color or '').lower() for s in slots if s.color}
        for req_color in required:
            if req_color not in loaded_colors:
                warnings.append(f"Required color '{req_color}' not found in loaded filament slots")

    return {"filament_warnings": warnings, "printer_id": printer_id}


@router.post("/bulk", response_model=List[JobResponse], status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_jobs_bulk(jobs: List[JobCreate], current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create multiple jobs at once."""
    from modules.models_library.services import calculate_job_cost

    # Pre-load org settings for default filament
    org_settings = {}
    if current_user and current_user.get("group_id"):
        from core.registry import registry
        _org_provider = registry.get_provider("OrgSettingsProvider")
        if _org_provider:
            org_settings = _org_provider.get_org_settings(db, current_user["group_id"])

    db_jobs = []
    for job in jobs:
        # Calculate cost if model is linked
        estimated_cost, suggested_price, _ = (None, None, None)
        if job.model_id:
            estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=job.model_id)

        effective_filament_type = job.filament_type
        effective_colors = job.colors_required
        if not effective_filament_type and org_settings.get("default_filament_type"):
            effective_filament_type = org_settings["default_filament_type"]
        if not effective_colors and org_settings.get("default_filament_color"):
            effective_colors = org_settings["default_filament_color"]

        db_job = Job(
            item_name=job.item_name,
            model_id=job.model_id,
            quantity=job.quantity,
            priority=job.priority,
            duration_hours=job.duration_hours,
            colors_required=effective_colors,
            filament_type=effective_filament_type,
            notes=job.notes,
            hold=job.hold,
            status=JobStatus.PENDING,
            estimated_cost=estimated_cost,
            suggested_price=suggested_price
        )
        db.add(db_job)
        db_jobs.append(db_job)

    db.commit()
    for job in db_jobs:
        db.refresh(job)
    return db_jobs


@router.post("/batch", tags=["Jobs"])
def create_jobs_batch(
    body: JobBatchRequest,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Send the same job to multiple printers at once (batch production).

    Creates one job per printer_id and returns all created jobs.
    """
    from modules.models_library.services import calculate_job_cost

    if not body.printer_ids:
        raise HTTPException(status_code=400, detail="printer_ids cannot be empty")
    if len(body.printer_ids) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 printers per batch")

    # Validate all printers exist
    printers = db.query(Printer).filter(Printer.id.in_(body.printer_ids)).all()
    found_ids = {p.id for p in printers}
    missing = set(body.printer_ids) - found_ids
    if missing:
        raise HTTPException(status_code=400, detail=f"Printer IDs not found: {sorted(missing)}")

    estimated_cost, suggested_price = None, None
    if body.model_id:
        estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=body.model_id)

    created = []
    for pid in body.printer_ids:
        db_job = Job(
            item_name=body.item_name,
            model_id=body.model_id,
            quantity=1,
            priority=body.priority,
            printer_id=pid,
            duration_hours=body.duration_hours,
            colors_required=body.colors_required,
            filament_type=body.filament_type,
            notes=body.notes,
            hold=body.queue_only,
            status=JobStatus.PENDING,
            estimated_cost=estimated_cost,
            suggested_price=suggested_price,
            charged_to_user_id=current_user.get("id"),
            charged_to_org_id=current_user.get("group_id"),
        )
        db.add(db_job)
        created.append(db_job)

    db.commit()
    return [{"id": j.id, "printer_id": j.printer_id, "status": j.status.value if hasattr(j.status, 'value') else str(j.status)} for j in created]


# Static route registered before /jobs/{job_id} to prevent FastAPI from
# treating "reorder" as a job_id integer.
@router.patch("/reorder", tags=["Jobs"])
async def reorder_jobs_static(req: JobReorderRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Reorder job queue. Sets queue_position on each job based on array index."""
    for position, job_id in enumerate(req.job_ids):
        db.execute(
            text("UPDATE jobs SET queue_position = :pos WHERE id = :id AND status IN ('pending', 'scheduled')"),
            {"pos": position, "id": job_id}
        )
    db.commit()
    return {"reordered": len(req.job_ids)}


@router.post("/bulk-update", tags=["Jobs"])
async def bulk_update_jobs(body: dict, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Bulk update job fields (status, priority) for multiple jobs."""
    job_ids = body.get("job_ids", [])
    if not job_ids or not isinstance(job_ids, list):
        raise HTTPException(status_code=400, detail="job_ids list is required")
    if len(job_ids) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 jobs per batch")

    action = body.get("action", "")
    count = 0

    if action == "cancel":
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if job and not check_org_access(current_user, job.charged_to_org_id):
                continue
            db.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :id AND status IN ('pending','submitted')"),
                       {"id": jid})
            count += 1
    elif action == "set_priority":
        priority = body.get("priority", 3)
        if priority not in range(1, 6):
            raise HTTPException(status_code=400, detail="Priority must be 1-5")
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if job and not check_org_access(current_user, job.charged_to_org_id):
                continue
            db.execute(text("UPDATE jobs SET priority = :p WHERE id = :id"), {"p": priority, "id": jid})
            count += 1
    elif action == "delete":
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if job and not check_org_access(current_user, job.charged_to_org_id):
                continue
            db.execute(text("DELETE FROM jobs WHERE id = :id AND status IN ('pending','submitted','cancelled')"),
                       {"id": jid})
            count += 1
    elif action == "hold":
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if job and not check_org_access(current_user, job.charged_to_org_id):
                continue
            db.execute(text("UPDATE jobs SET hold = 1 WHERE id = :id"), {"id": jid})
            count += 1
    elif action == "unhold":
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if job and not check_org_access(current_user, job.charged_to_org_id):
                continue
            db.execute(text("UPDATE jobs SET hold = 0 WHERE id = :id"), {"id": jid})
            count += 1
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    db.commit()
    log_audit(db, f"bulk_{action}", "jobs", details=f"{count} jobs")
    return {"status": "ok", "affected": count}


@router.get("", response_model=List[JobResponse], tags=["Jobs"])
def list_jobs(
    status: Optional[JobStatus] = None,
    printer_id: Optional[int] = None,
    org_id: Optional[int] = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db)
):
    """List jobs with optional filters."""
    query = db.query(Job)

    if status:
        query = query.filter(Job.status == status)
    if printer_id:
        query = query.filter(Job.printer_id == printer_id)

    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if effective_org is not None:
        query = query.filter((Job.charged_to_org_id == effective_org) | (Job.charged_to_org_id == None))

    return query.order_by(Job.priority, Job.created_at).offset(offset).limit(limit).all()


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_job(job: JobCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """Create a new print job. If approval is required and user is a viewer, job is created as 'submitted'.

    require_role("viewer") allows viewers, operators, and admins — blocking only unauthenticated
    requests. The approval workflow logic checks current_user["role"] internally.
    """
    # Import here to avoid circular at module level
    from modules.models_library.services import calculate_job_cost

    # Check print quota before creating job
    if current_user:
        quota_jobs = current_user.get("quota_jobs")
        if quota_jobs is not None and quota_jobs > 0:
            period = current_user.get("quota_period") or "monthly"
            usage = _get_quota_usage(db, current_user["id"], period)
            if usage["jobs_used"] >= quota_jobs:
                raise HTTPException(status_code=429, detail=f"Job quota exceeded ({usage['jobs_used']}/{quota_jobs} for this {period} period)")

    # Calculate cost if model is linked
    estimated_cost, suggested_price, _ = (None, None, None)
    if job.model_id:
        estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=job.model_id)

    # Check if approval workflow is enabled
    approval_required = False
    approval_config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    if approval_config and approval_config.value in (True, "true", "True", "1"):
        approval_required = True

    # Determine initial status
    initial_status = JobStatus.PENDING
    submitted_by = None
    if approval_required and current_user and current_user.get("role") == "viewer":
        initial_status = "submitted"
        submitted_by = current_user.get("id")

    # Resolve model_revision_id: use provided value, or default to latest revision
    model_revision_id = getattr(job, 'model_revision_id', None)
    if model_revision_id is None and job.model_id:
        latest_rev = db.execute(text(
            "SELECT id FROM model_revisions WHERE model_id = :mid ORDER BY revision_number DESC LIMIT 1"),
            {"mid": job.model_id}).fetchone()
        if latest_rev:
            model_revision_id = latest_rev.id

    # Apply org default filament if not explicitly set
    effective_filament_type = job.filament_type
    effective_colors = job.colors_required
    if current_user and current_user.get("group_id") and not effective_filament_type:
        from core.registry import registry
        _org_provider = registry.get_provider("OrgSettingsProvider")
        if _org_provider:
            org_settings = _org_provider.get_org_settings(db, current_user["group_id"])
            if org_settings.get("default_filament_type"):
                effective_filament_type = org_settings["default_filament_type"]
            if not effective_colors and org_settings.get("default_filament_color"):
                effective_colors = org_settings["default_filament_color"]

    db_job = Job(
        item_name=job.item_name,
        model_id=job.model_id,
        model_revision_id=model_revision_id,
        quantity=job.quantity,
        priority=job.priority,
        duration_hours=job.duration_hours,
        colors_required=effective_colors,
        filament_type=effective_filament_type,
        notes=job.notes,
        hold=job.hold,
        status=initial_status,
        estimated_cost=estimated_cost,
        suggested_price=suggested_price,
        submitted_by=submitted_by,
        due_date=job.due_date,
        charged_to_user_id=current_user["id"] if current_user else None,
        charged_to_org_id=current_user.get("group_id") if current_user else None,
        required_tags=job.required_tags or [],
        target_type=job.target_type or "specific",
        target_filter=job.target_filter,
    )
    db.add(db_job)
    db.commit()
    db.refresh(db_job)

    # Increment quota usage
    if current_user and current_user.get("quota_jobs"):
        period = current_user.get("quota_period") or "monthly"
        pk = _get_period_key(period)
        db.execute(text("""INSERT INTO quota_usage (user_id, period_key, jobs_used)
                           VALUES (:uid, :pk, 1)
                           ON CONFLICT(user_id, period_key) DO UPDATE SET jobs_used = jobs_used + 1, updated_at = CURRENT_TIMESTAMP"""),
                   {"uid": current_user["id"], "pk": pk})
        db.commit()

    # If submitted for approval, notify group owner (or all operators/admins as fallback)
    if initial_status == "submitted":
        try:
            from modules.notifications.alert_dispatcher import dispatch_alert, get_group_owner_id, get_operator_admin_ids
            owner_id = get_group_owner_id(db, current_user["id"])
            target_ids = [owner_id] if owner_id else get_operator_admin_ids(db)
            dispatch_alert(
                db=db,
                alert_type=AlertType.JOB_SUBMITTED,
                severity=AlertSeverity.INFO,
                title=f"Job awaiting approval: {job.item_name or 'Untitled'}",
                message=f"{current_user.get('display_name') or current_user.get('username', 'A user')} submitted a print job",
                job_id=db_job.id,
                target_user_ids=target_ids,
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch job_submitted alert: {e}")

    return db_job


@router.get("/{job_id}", response_model=JobResponse, tags=["Jobs"])
def get_job(job_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get a specific job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.patch("/{job_id}", response_model=JobResponse, tags=["Jobs"])
def update_job(job_id: int, updates: JobUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    for field, value in updates.model_dump(exclude_unset=True).items():
        setattr(job, field, value)

    db.commit()
    db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Jobs"])
def delete_job(job_id: int, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Delete a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    db.delete(job)
    db.commit()


@router.post("/{job_id}/repeat", tags=["Jobs"])
async def repeat_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Clone a job for printing again. Creates a new pending job with same settings."""
    original = db.query(Job).filter(Job.id == job_id).first()
    if not original:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, original.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    # Create new job with same settings
    new_job = Job(
        model_id=original.model_id,
        item_name=original.item_name,
        quantity=original.quantity,
        status="pending",
        priority=original.priority,
        printer_id=original.printer_id,  # Same printer preference
        duration_hours=original.duration_hours,
        colors_required=original.colors_required,
        filament_type=original.filament_type,
        notes=f"Repeat of job #{job_id}" + (f" - {original.notes}" if original.notes else ""),
        estimated_cost=original.estimated_cost,
        suggested_price=original.suggested_price,
        quantity_on_bed=original.quantity_on_bed,
    )

    db.add(new_job)
    db.commit()
    db.refresh(new_job)

    return {
        "success": True,
        "message": f"Job cloned successfully",
        "original_job_id": job_id,
        "new_job_id": new_job.id
    }
