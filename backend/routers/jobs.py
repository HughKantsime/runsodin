"""O.D.I.N. — Job Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
from datetime import datetime, timedelta
from pydantic import BaseModel as PydanticBaseModel
import json
import logging

from deps import (get_db, get_current_user, require_role, log_audit,
                  _get_org_filter, get_org_scope, check_org_access,
                  compute_printer_online)
from models import (
    Job, JobStatus, Printer, Model, Spool, SpoolUsage, SpoolStatus,
    SystemConfig, AlertType, AlertSeverity, FilamentType, PrintPreset,
)
from schemas import (
    JobCreate, JobUpdate, JobResponse, JobSummary,
)
from config import settings
from license_manager import require_feature

log = logging.getLogger("odin.api")
logger = log

router = APIRouter()


# ──────────────────────────────────────────────
# Helpers (used only by jobs)
# ──────────────────────────────────────────────

def _get_period_key(period: str) -> str:
    """Generate a period key like '2026-02' for monthly, '2026-W07' for weekly."""
    now = datetime.now()
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    elif period == "weekly":
        return now.strftime("%Y-W%W")
    elif period == "semester":
        return f"{now.year}-S{'1' if now.month <= 6 else '2'}"
    else:  # monthly
        return now.strftime("%Y-%m")


def _get_quota_usage(db, user_id, period):
    """Get or create quota usage row for current period."""
    key = _get_period_key(period)
    row = db.execute(text("SELECT * FROM quota_usage WHERE user_id = :uid AND period_key = :pk"),
                     {"uid": user_id, "pk": key}).fetchone()
    if row:
        return dict(row._mapping)
    db.execute(text("INSERT INTO quota_usage (user_id, period_key) VALUES (:uid, :pk)"),
               {"uid": user_id, "pk": key})
    db.commit()
    return {"user_id": user_id, "period_key": key, "grams_used": 0, "hours_used": 0, "jobs_used": 0}


FAILURE_REASONS = [
    {"value": "spaghetti", "label": "Spaghetti / Detached"},
    {"value": "adhesion", "label": "Bed Adhesion Failure"},
    {"value": "clog", "label": "Nozzle Clog"},
    {"value": "layer_shift", "label": "Layer Shift"},
    {"value": "stringing", "label": "Excessive Stringing"},
    {"value": "warping", "label": "Warping / Curling"},
    {"value": "filament_runout", "label": "Filament Runout"},
    {"value": "filament_tangle", "label": "Filament Tangle"},
    {"value": "power_loss", "label": "Power Loss"},
    {"value": "firmware_error", "label": "Firmware / HMS Error"},
    {"value": "user_cancelled", "label": "User Cancelled"},
    {"value": "other", "label": "Other"},
]


# ──────────────────────────────────────────────
# Jobs CRUD
# ──────────────────────────────────────────────

@router.get("/jobs", response_model=List[JobResponse], tags=["Jobs"])
def list_jobs(
    status: Optional[JobStatus] = None,
    printer_id: Optional[int] = None,
    org_id: Optional[int] = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
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


@router.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_job(job: JobCreate, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Create a new print job. If approval is required and user is a viewer, job is created as 'submitted'.

    Note: No require_role() here -- intentional. Viewers can create jobs that enter the approval
    workflow (status='submitted') when require_job_approval is enabled. The approval flow handles
    authorization; operators/admins bypass it and create jobs directly as 'pending'.
    """
    # Import here to avoid circular at module level
    from routers.models import calculate_job_cost

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

    db_job = Job(
        item_name=job.item_name,
        model_id=job.model_id,
        model_revision_id=model_revision_id,
        quantity=job.quantity,
        priority=job.priority,
        duration_hours=job.duration_hours,
        colors_required=job.colors_required,
        filament_type=job.filament_type,
        notes=job.notes,
        hold=job.hold,
        status=initial_status,
        estimated_cost=estimated_cost,
        suggested_price=suggested_price,
        submitted_by=submitted_by,
        due_date=job.due_date,
        charged_to_user_id=current_user["id"] if current_user else None,
        charged_to_org_id=current_user.get("group_id") if current_user else None,
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
            from alert_dispatcher import dispatch_alert, get_group_owner_id, get_operator_admin_ids
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


@router.post("/jobs/bulk", response_model=List[JobResponse], status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_jobs_bulk(jobs: List[JobCreate], current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create multiple jobs at once."""
    from routers.models import calculate_job_cost

    db_jobs = []
    for job in jobs:
        # Calculate cost if model is linked
        estimated_cost, suggested_price, _ = (None, None, None)
        if job.model_id:
            estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=job.model_id)

        db_job = Job(
            item_name=job.item_name,
            model_id=job.model_id,
            quantity=job.quantity,
            priority=job.priority,
            duration_hours=job.duration_hours,
            colors_required=job.colors_required,
            filament_type=job.filament_type,
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


@router.get("/jobs/{job_id}", response_model=JobResponse, tags=["Jobs"])
def get_job(job_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get a specific job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if current_user and not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.patch("/jobs/{job_id}", response_model=JobResponse, tags=["Jobs"])
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


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Jobs"])
def delete_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    db.delete(job)
    db.commit()


@router.post("/jobs/{job_id}/repeat", tags=["Jobs"])
async def repeat_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Clone a job for printing again. Creates a new pending job with same settings."""
    original = db.query(Job).filter(Job.id == job_id).first()
    if not original:
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


# ──────────────────────────────────────────────
# Job lifecycle
# ──────────────────────────────────────────────

@router.post("/jobs/{job_id}/start", response_model=JobResponse, tags=["Jobs"])
def start_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Mark a job as started (printing)."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in [JobStatus.SCHEDULED, JobStatus.PENDING]:
        raise HTTPException(status_code=400, detail=f"Cannot start job in {job.status} status")

    job.status = JobStatus.PRINTING
    job.actual_start = datetime.utcnow()
    job.is_locked = True

    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs/{job_id}/complete", response_model=JobResponse, tags=["Jobs"])
def complete_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Mark a job as completed and auto-deduct filament from loaded spools."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.COMPLETED
    job.actual_end = datetime.utcnow()
    job.is_locked = True

    # Update printer's loaded colors based on this job
    if job.printer_id and job.colors_list:
        printer = db.query(Printer).filter(Printer.id == job.printer_id).first()
        if printer:
            for i, color in enumerate(job.colors_list[:printer.slot_count]):
                slot = next((s for s in printer.filament_slots if s.slot_number == i + 1), None)
                if slot:
                    slot.color = color

    # ---- Auto-deduct filament from spools ----
    deductions = []
    slot_grams = {}  # {slot_number: grams_to_deduct}

    # Source 1: Model color_requirements (has per-slot gram amounts)
    if job.model_id:
        model = db.query(Model).filter(Model.id == job.model_id).first()
        if model and model.color_requirements:
            req = model.color_requirements if isinstance(model.color_requirements, dict) else json.loads(model.color_requirements)
            for i, slot_key in enumerate(sorted(req.keys())):
                slot_data = req[slot_key]
                if isinstance(slot_data, dict) and slot_data.get("grams"):
                    slot_grams[i + 1] = float(slot_data["grams"])

    # Source 2: Linked print file filaments (fallback if model has no gram data)
    if not slot_grams:
        # Check if a print_file is linked to this job
        pf_row = db.execute(text(
            "SELECT filaments_json FROM print_files WHERE job_id = :jid LIMIT 1"
        ), {"jid": job.id}).first()
        if pf_row and pf_row[0]:
            try:
                pf_filaments = json.loads(pf_row[0]) if isinstance(pf_row[0], str) else pf_row[0]
                for i, fil in enumerate(pf_filaments):
                    grams = fil.get("used_grams") or fil.get("weight_grams")
                    if grams:
                        slot_grams[i + 1] = float(grams)
            except (json.JSONDecodeError, TypeError):
                pass

    # Apply deductions to spools loaded on this printer
    if slot_grams and job.printer_id:
        loaded_spools = db.query(Spool).filter(
            Spool.location_printer_id == job.printer_id,
            Spool.status == SpoolStatus.ACTIVE
        ).all()

        spool_by_slot = {s.location_slot: s for s in loaded_spools if s.location_slot}

        for slot_num, grams in slot_grams.items():
            spool = spool_by_slot.get(slot_num)
            if spool and grams > 0:
                old_weight = spool.remaining_weight_g
                spool.remaining_weight_g = max(0, spool.remaining_weight_g - grams)

                # Create usage record for audit trail
                usage = SpoolUsage(
                    spool_id=spool.id,
                    weight_used_g=grams,
                    job_id=job.id,
                    notes=f"Auto-deducted on job #{job.id} complete ({job.item_name})"
                )
                db.add(usage)

                deductions.append({
                    "spool_id": spool.id,
                    "slot": slot_num,
                    "deducted_g": round(grams, 1),
                    "remaining_g": round(spool.remaining_weight_g, 1)
                })

    if deductions:
        deduct_summary = "; ".join(
            f"Slot {d['slot']}: -{d['deducted_g']}g (spool #{d['spool_id']})"
            for d in deductions
        )
        job.notes = f"{job.notes or ''}\nFilament deducted: {deduct_summary}".strip()

    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs/{job_id}/fail", response_model=JobResponse, tags=["Jobs"])
def fail_job(job_id: int, notes: Optional[str] = None, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Mark a job as failed."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.FAILED
    job.actual_end = datetime.utcnow()
    job.is_locked = True
    if notes:
        job.notes = f"{job.notes or ''}\nFailed: {notes}".strip()

    db.commit()
    db.refresh(job)
    return job

@router.post("/jobs/{job_id}/cancel", response_model=JobResponse, tags=["Jobs"])
def cancel_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Cancel a pending or scheduled job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in [JobStatus.PENDING, JobStatus.SCHEDULED]:
        raise HTTPException(status_code=400, detail="Can only cancel pending or scheduled jobs")
    job.status = JobStatus.CANCELLED
    job.is_locked = True
    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs/{job_id}/reset", response_model=JobResponse, tags=["Jobs"])
def reset_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Reset a job back to pending status."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.PENDING
    job.printer_id = None
    job.scheduled_start = None
    job.scheduled_end = None
    job.actual_start = None
    job.actual_end = None
    job.match_score = None
    job.is_locked = False

    db.commit()
    db.refresh(job)
    return job


# ──────────────────────────────────────────────
# Job Approval Workflow (v0.18.0)
# ──────────────────────────────────────────────

class _RejectJobRequest(PydanticBaseModel):
    """Inline schema for reject endpoint."""
    reason: str

@router.post("/jobs/{job_id}/approve", tags=["Jobs"])
def approve_job(job_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    require_feature("job_approval")
    """Approve a submitted job. Moves it to pending status for scheduling."""
    if not current_user or current_user.get("role") not in ("operator", "admin"):
        raise HTTPException(status_code=403, detail="Only operators and admins can approve jobs")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "submitted":
        raise HTTPException(status_code=400, detail="Job is not in submitted status")

    job.status = JobStatus.PENDING
    job.approved_by = current_user["id"]
    job.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(job)

    # Notify the student who submitted
    if job.submitted_by:
        try:
            from alert_dispatcher import dispatch_alert
            dispatch_alert(
                db=db,
                alert_type=AlertType.JOB_APPROVED,
                severity=AlertSeverity.INFO,
                title=f"Job approved: {job.item_name or 'Untitled'}",
                message=f"Approved by {current_user.get('display_name') or current_user.get('username', 'an approver')}",
                job_id=job.id,
                target_user_ids=[job.submitted_by],
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch job_approved alert: {e}")

    return {"status": "approved", "job_id": job.id}


@router.post("/jobs/{job_id}/reject", tags=["Jobs"])
def reject_job(job_id: int, body: _RejectJobRequest, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    require_feature("job_approval")
    """Reject a submitted job with a required reason."""
    if not current_user or current_user.get("role") not in ("operator", "admin"):
        raise HTTPException(status_code=403, detail="Only operators and admins can reject jobs")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "submitted":
        raise HTTPException(status_code=400, detail="Job is not in submitted status")

    if not body.reason or not body.reason.strip():
        raise HTTPException(status_code=400, detail="Rejection reason is required")

    job.status = "rejected"
    job.approved_by = current_user["id"]
    job.rejected_reason = body.reason.strip()
    db.commit()
    db.refresh(job)

    # Notify the student who submitted
    if job.submitted_by:
        try:
            from alert_dispatcher import dispatch_alert
            dispatch_alert(
                db=db,
                alert_type=AlertType.JOB_REJECTED,
                severity=AlertSeverity.WARNING,
                title=f"Job rejected: {job.item_name or 'Untitled'}",
                message=f"Reason: {body.reason.strip()}",
                job_id=job.id,
                target_user_ids=[job.submitted_by],
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch job_rejected alert: {e}")

    return {"status": "rejected", "job_id": job.id, "reason": body.reason.strip()}


@router.post("/jobs/{job_id}/resubmit", tags=["Jobs"])
def resubmit_job(job_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    require_feature("job_approval")
    """Resubmit a rejected job for approval again."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "rejected":
        raise HTTPException(status_code=400, detail="Only rejected jobs can be resubmitted")

    if job.submitted_by != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the original submitter can resubmit")

    job.status = "submitted"
    job.rejected_reason = None
    job.approved_by = None
    job.approved_at = None
    db.commit()
    db.refresh(job)

    # Re-notify group owner (or all operators/admins as fallback)
    try:
        from alert_dispatcher import dispatch_alert, get_group_owner_id, get_operator_admin_ids
        owner_id = get_group_owner_id(db, current_user["id"])
        target_ids = [owner_id] if owner_id else get_operator_admin_ids(db)
        dispatch_alert(
            db=db,
            alert_type=AlertType.JOB_SUBMITTED,
            severity=AlertSeverity.INFO,
            title=f"Job resubmitted: {job.item_name or 'Untitled'}",
            message=f"{current_user.get('display_name') or current_user.get('username', 'A user')} resubmitted a previously rejected job",
            job_id=job.id,
            target_user_ids=target_ids,
        )
    except Exception as e:
        logger.warning(f"Failed to dispatch job_submitted alert: {e}")

    return {"status": "resubmitted", "job_id": job.id}


# ──────────────────────────────────────────────
# Config: require-job-approval
# ──────────────────────────────────────────────

@router.get("/config/require-job-approval", tags=["Config"])
def get_approval_setting(db: Session = Depends(get_db)):
    """Get the current job approval requirement setting."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    enabled = False
    if config and config.value in (True, "true", "True", "1"):
        enabled = True
    return {"require_job_approval": enabled}


@router.put("/config/require-job-approval", tags=["Config"])
def set_approval_setting(body: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Toggle the job approval requirement. Admin only. Requires Education tier."""
    require_feature("job_approval")
    enabled = body.get("enabled", False)
    config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    if config:
        config.value = "true" if enabled else "false"
    else:
        config = SystemConfig(key="require_job_approval", value="true" if enabled else "false")
        db.add(config)
    db.commit()
    return {"require_job_approval": enabled}


# ──────────────────────────────────────────────
# Link job to MQTT print
# ──────────────────────────────────────────────

@router.post("/jobs/{job_id}/link-print", tags=["Jobs"])
def link_job_to_print(job_id: int, print_job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Link a scheduled job to an MQTT-detected print."""
    # Check job exists
    job = db.execute(text("SELECT id, printer_id FROM jobs WHERE id = :id"), {"id": job_id}).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check print_job exists
    print_job = db.execute(text("SELECT id, printer_id, status FROM print_jobs WHERE id = :id"), {"id": print_job_id}).fetchone()
    if not print_job:
        raise HTTPException(status_code=404, detail="Print job not found")

    # Verify same printer
    if job.printer_id != print_job.printer_id:
        raise HTTPException(status_code=400, detail="Job and print are on different printers")

    # Link them
    db.execute(text("UPDATE print_jobs SET scheduled_job_id = :job_id WHERE id = :id"),
               {"job_id": job_id, "id": print_job_id})

    # Update job status based on print status
    new_status = None
    if print_job.status == 'completed':
        new_status = 'completed'
    elif print_job.status == 'failed':
        new_status = 'failed'
    elif print_job.status in ('printing', 'running'):
        new_status = 'printing'

    if new_status:
        db.execute(text("UPDATE jobs SET status = :status WHERE id = :id"),
                   {"status": new_status, "id": job_id})

    db.commit()
    return {"message": "Linked", "job_id": job_id, "print_job_id": print_job_id}


@router.get("/print-jobs/unlinked", tags=["Print Jobs"])
def get_unlinked_print_jobs(printer_id: int = None, db: Session = Depends(get_db)):
    """Get recent print jobs not linked to scheduled jobs."""
    sql = """
        SELECT pj.*, p.name as printer_name
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        WHERE pj.scheduled_job_id IS NULL
    """
    params = {}

    if printer_id:
        sql += " AND pj.printer_id = :printer_id"
        params["printer_id"] = printer_id

    sql += " ORDER BY pj.started_at DESC LIMIT 20"

    result = db.execute(text(sql), params).fetchall()
    return [dict(row._mapping) for row in result]


# ──────────────────────────────────────────────
# Move job
# ──────────────────────────────────────────────

class MoveJobRequest(PydanticBaseModel):
    printer_id: int
    scheduled_start: datetime

@router.patch("/jobs/{job_id}/move", tags=["Jobs"])
def move_job(
    job_id: int,
    request: MoveJobRequest,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Move a job to a different printer and/or time slot."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Verify printer exists
    printer = db.query(Printer).filter(Printer.id == request.printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Calculate end time based on job duration
    duration_hours = job.effective_duration
    scheduled_end = request.scheduled_start + timedelta(hours=duration_hours)

    # Check for conflicts (other jobs on same printer overlapping this time)
    conflict = db.query(Job).filter(
        Job.id != job_id,
        Job.printer_id == request.printer_id,
        Job.status.in_([JobStatus.SCHEDULED, JobStatus.PRINTING]),
        Job.scheduled_start < scheduled_end,
        Job.scheduled_end > request.scheduled_start
    ).first()

    if conflict:
        raise HTTPException(
            status_code=400,
            detail=f"Time slot conflicts with job '{conflict.item_name}'"
        )

    # Update the job
    job.printer_id = request.printer_id
    job.scheduled_start = request.scheduled_start
    job.scheduled_end = scheduled_end
    if job.status == JobStatus.PENDING:
        job.status = JobStatus.SCHEDULED

    db.commit()
    db.refresh(job)

    return {
        "success": True,
        "job_id": job.id,
        "printer_id": request.printer_id,
        "scheduled_start": request.scheduled_start.isoformat(),
        "scheduled_end": scheduled_end.isoformat()
    }


# ──────────────────────────────────────────────
# Print jobs (MQTT-tracked)
# ──────────────────────────────────────────────

@router.get("/print-jobs", tags=["Print Jobs"])
def get_print_jobs(
    printer_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db)
):
    """Get print job history from MQTT tracking."""
    from datetime import datetime as dt

    # Build query dynamically
    sql = """
        SELECT pj.*, p.name as printer_name
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        WHERE 1=1
    """
    params = {}

    if printer_id is not None:
        sql += " AND pj.printer_id = :printer_id"
        params["printer_id"] = printer_id
    if status is not None:
        sql += " AND pj.status = :status"
        params["status"] = status

    sql += " ORDER BY pj.started_at DESC LIMIT :limit"
    params["limit"] = limit

    result = db.execute(text(sql), params).fetchall()

    jobs = []
    for row in result:
        job = dict(row._mapping)
        if job.get('ended_at') and job.get('started_at'):
            try:
                start = dt.fromisoformat(job['started_at'])
                end = dt.fromisoformat(job['ended_at'])
                job['duration_minutes'] = round((end - start).total_seconds() / 60, 1)
            except:
                job['duration_minutes'] = None
        else:
            job['duration_minutes'] = None
        jobs.append(job)

    return jobs

@router.get("/print-jobs/stats", tags=["Print Jobs"])
def get_print_job_stats(db: Session = Depends(get_db)):
    """Get aggregated print job statistics."""
    query = text("""
        SELECT
            p.id as printer_id,
            p.name as printer_name,
            COUNT(*) as total_jobs,
            SUM(CASE WHEN pj.status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,
            SUM(CASE WHEN pj.status = 'failed' THEN 1 ELSE 0 END) as failed_jobs,
            SUM(CASE WHEN pj.status = 'running' THEN 1 ELSE 0 END) as running_jobs,
            ROUND(SUM(
                CASE WHEN pj.ended_at IS NOT NULL
                THEN (julianday(pj.ended_at) - julianday(pj.started_at)) * 24
                ELSE 0 END
            ), 2) as total_hours
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        GROUP BY p.id
        ORDER BY total_hours DESC
    """)
    result = db.execute(query).fetchall()
    return [dict(row._mapping) for row in result]


# ──────────────────────────────────────────────
# Bulk update jobs
# ──────────────────────────────────────────────

@router.post("/jobs/bulk-update", tags=["Jobs"])
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
            db.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :id AND status IN ('pending','submitted')"),
                       {"id": jid})
            count += 1
    elif action == "set_priority":
        priority = body.get("priority", 3)
        if priority not in range(1, 6):
            raise HTTPException(status_code=400, detail="Priority must be 1-5")
        for jid in job_ids:
            db.execute(text("UPDATE jobs SET priority = :p WHERE id = :id"), {"p": priority, "id": jid})
            count += 1
    elif action == "delete":
        for jid in job_ids:
            db.execute(text("DELETE FROM jobs WHERE id = :id AND status IN ('pending','submitted','cancelled')"),
                       {"id": jid})
            count += 1
    elif action == "hold":
        for jid in job_ids:
            db.execute(text("UPDATE jobs SET hold = 1 WHERE id = :id"), {"id": jid})
            count += 1
    elif action == "unhold":
        for jid in job_ids:
            db.execute(text("UPDATE jobs SET hold = 0 WHERE id = :id"), {"id": jid})
            count += 1
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    db.commit()
    log_audit(db, f"bulk_{action}", "jobs", details=f"{count} jobs")
    return {"status": "ok", "affected": count}


# ──────────────────────────────────────────────
# Reorder jobs
# ──────────────────────────────────────────────

class JobReorderRequest(PydanticBaseModel):
    job_ids: list[int]  # Ordered list of job IDs in desired queue position

@router.patch("/jobs/reorder", tags=["Jobs"])
async def reorder_jobs(req: JobReorderRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """
    Reorder job queue. Accepts ordered list of job IDs.
    Sets queue_position on each job based on array index.
    Only reorders pending/scheduled jobs.
    """
    for position, job_id in enumerate(req.job_ids):
        db.execute(
            text("UPDATE jobs SET queue_position = :pos WHERE id = :id AND status IN ('pending', 'scheduled')"),
            {"pos": position, "id": job_id}
        )
    db.commit()
    return {"reordered": len(req.job_ids)}


# ──────────────────────────────────────────────
# Failure reasons
# ──────────────────────────────────────────────

@router.get("/failure-reasons", tags=["Jobs"])
async def get_failure_reasons():
    """List available failure reason categories."""
    return FAILURE_REASONS


@router.patch("/jobs/{job_id}/failure", tags=["Jobs"])
async def update_job_failure(
    job_id: int,
    request: Request,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db)
):
    """Add or update failure reason and notes on a failed job."""
    data = await request.json()

    job = db.execute(text("SELECT id, status FROM jobs WHERE id = :id"), {"id": job_id}).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job_dict = dict(job._mapping)
    if job_dict["status"] != "failed":
        raise HTTPException(status_code=400, detail="Can only add failure info to failed jobs")

    fail_reason = data.get("fail_reason")
    fail_notes = data.get("fail_notes")

    updates = []
    params = {"id": job_id}

    if fail_reason is not None:
        updates.append("fail_reason = :reason")
        params["reason"] = fail_reason

    if fail_notes is not None:
        updates.append("fail_notes = :notes")
        params["notes"] = fail_notes

    if updates:
        updates.append("updated_at = datetime('now')")
        db.execute(text(f"UPDATE jobs SET {', '.join(updates)} WHERE id = :id"), params)
        db.commit()

    return {"success": True, "message": "Failure info updated"}


# ──────────────────────────────────────────────
# Print Presets
# ──────────────────────────────────────────────

@router.get("/presets", tags=["Presets"])
def list_presets(db: Session = Depends(get_db)):
    """List all print presets."""
    presets = db.query(PrintPreset).order_by(PrintPreset.name).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "model_id": p.model_id,
            "model_name": p.model.name if p.model else None,
            "item_name": p.item_name,
            "quantity": p.quantity,
            "priority": p.priority,
            "duration_hours": p.duration_hours,
            "colors_required": p.colors_required,
            "filament_type": p.filament_type.value if p.filament_type else None,
            "required_tags": p.required_tags or [],
            "notes": p.notes,
        }
        for p in presets
    ]


@router.post("/presets", tags=["Presets"], status_code=status.HTTP_201_CREATED)
def create_preset(
    request_data: dict,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Create a new print preset."""
    preset = PrintPreset(
        name=request_data["name"],
        model_id=request_data.get("model_id"),
        item_name=request_data.get("item_name"),
        quantity=request_data.get("quantity", 1),
        priority=request_data.get("priority", 3),
        duration_hours=request_data.get("duration_hours"),
        colors_required=request_data.get("colors_required"),
        filament_type=request_data.get("filament_type"),
        required_tags=request_data.get("required_tags", []),
        notes=request_data.get("notes"),
    )
    db.add(preset)
    db.commit()
    db.refresh(preset)
    return {"id": preset.id, "name": preset.name}


@router.delete("/presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Presets"])
def delete_preset(
    preset_id: int,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Delete a print preset."""
    preset = db.query(PrintPreset).filter(PrintPreset.id == preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")
    db.delete(preset)
    db.commit()


@router.post("/presets/{preset_id}/schedule", tags=["Presets"])
def schedule_from_preset(
    preset_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new job from a preset."""
    preset = db.query(PrintPreset).filter(PrintPreset.id == preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    job = Job(
        item_name=preset.item_name or preset.name,
        model_id=preset.model_id,
        quantity=preset.quantity or 1,
        priority=preset.priority or 3,
        duration_hours=preset.duration_hours,
        colors_required=preset.colors_required,
        filament_type=preset.filament_type,
        required_tags=preset.required_tags or [],
        notes=preset.notes,
    )

    # Cost calculation from model if available
    if preset.model_id:
        model = db.query(Model).filter(Model.id == preset.model_id).first()
        if model:
            if not job.duration_hours and model.build_time_hours:
                job.duration_hours = model.build_time_hours
            if model.estimated_cost:
                job.estimated_cost = model.estimated_cost
            if model.suggested_price:
                job.suggested_price = model.suggested_price

    db.add(job)
    db.commit()
    db.refresh(job)
    return {"id": job.id, "item_name": job.item_name, "status": job.status.value}
