"""Job lifecycle, approval workflow, dispatch, and print-job tracking."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from sqlalchemy import text, update, or_
from typing import Optional
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel as PydanticBaseModel
import json
import logging

from core.db import get_db
from core.db_compat import sql
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, check_org_access
from core.base import JobStatus, AlertType, AlertSeverity
from modules.jobs.models import Job
from modules.printers.models import Printer
from modules.inventory.models import Spool, SpoolUsage
from modules.models_library.models import Model
from modules.jobs.schemas import JobResponse
from core.models import SystemConfig
from license_manager import require_feature

log = logging.getLogger("odin.api")
logger = log

router = APIRouter(prefix="/jobs", tags=["Jobs"])


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


class MoveJobRequest(PydanticBaseModel):
    printer_id: int
    scheduled_start: datetime


class _RejectJobRequest(PydanticBaseModel):
    """Inline schema for reject endpoint."""
    reason: str


@router.post("/{job_id}/start", response_model=JobResponse, tags=["Jobs"])
def start_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Mark a job as started (printing)."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in [JobStatus.SCHEDULED, JobStatus.PENDING]:
        raise HTTPException(status_code=400, detail=f"Cannot start job in {job.status} status")

    job.status = JobStatus.PRINTING
    job.actual_start = datetime.now(timezone.utc)
    job.is_locked = True

    log_audit(db, "job.started", "job", job.id, {"printer_id": job.printer_id})
    db.commit()
    db.refresh(job)
    return job


@router.post("/{job_id}/complete", response_model=JobResponse, tags=["Jobs"])
def complete_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Mark a job as completed and auto-deduct filament from loaded spools.

    Race-safe idempotency (R5 from 2026-04-12 adversarial review,
    verification round):

    The first revision of this fix used a `if job.status == COMPLETED:
    return` check. That's correct for serial retries but broken under
    concurrency — two threads can both read status!=COMPLETED, both pass
    the guard, both run deductions, both commit, and the spool is
    deducted twice.

    This revision replaces the Python-level check with an atomic
    conditional UPDATE: set status=COMPLETED WHERE id=:id AND
    status!=COMPLETED. The DB decides which caller wins. rowcount==0
    means the job was already completed (or didn't match) — return the
    current row unchanged. rowcount==1 means this call owns the
    transition and is safe to run the deduction path.

    Safe on both SQLite (single writer) and Postgres (atomic UPDATE).
    No reliance on with_for_update() or SQLite's BEGIN IMMEDIATE, both
    of which have been race-windows in practice.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    # R5: atomic state transition. The DB — not Python — enforces
    # single-winner semantics. The UPDATE's WHERE clause is the lock.
    #
    # Codex pass 2: include `Job.status IS NULL` in the predicate. Job.status
    # is not declared NOT NULL, so legacy/hand-written rows can have NULL
    # status. SQL three-valued logic means `NULL != 'completed'` is NULL
    # (not TRUE), so a bare `status != COMPLETED` would silently skip
    # those rows and `/complete` would return them un-completed — a
    # regression vs the previous behavior. or_(... IS NULL) covers them.
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(Job)
        .where(
            Job.id == job_id,
            or_(Job.status != JobStatus.COMPLETED, Job.status.is_(None)),
        )
        .values(
            status=JobStatus.COMPLETED,
            actual_end=now,
            is_locked=True,
        )
    )

    if result.rowcount == 0:
        # Already completed by another request (retry, concurrent operator,
        # or whatever). Return the job as-is with no side effects.
        db.commit()
        db.refresh(job)
        return job

    # We won the transition. Refresh so downstream sees the new state.
    db.refresh(job)

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
        from core.base import SpoolStatus
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
                    "remaining_g": round(spool.remaining_weight_g, 1),
                    # v1.8.5: carried through to Spoolman push-back below.
                    # None for spools not linked to an external Spoolman row.
                    "spoolman_spool_id": spool.spoolman_spool_id,
                    "grams": float(grams),
                    "job_id": job.id,
                })

    if deductions:
        deduct_summary = "; ".join(
            f"Slot {d['slot']}: -{d['deducted_g']}g (spool #{d['spool_id']})"
            for d in deductions
        )
        job.notes = f"{job.notes or ''}\nFilament deducted: {deduct_summary}".strip()

    log_audit(db, "job.completed", "job", job.id, {"printer_id": job.printer_id, "deductions": len(deductions)})
    db.commit()
    db.refresh(job)

    # v1.8.5: push consumption back to Spoolman for any deductions whose
    # spool has a spoolman_spool_id. Fires AFTER the local commit so the
    # authoritative spool state is persisted regardless of whether
    # Spoolman is reachable. Errors are surfaced to the operator via
    # job.notes; no silent drops, no retry-queue coupling.
    if deductions:
        try:
            from modules.inventory.services import push_consumption_to_spoolman
            push_errors = push_consumption_to_spoolman(deductions)
            if push_errors:
                for err in push_errors:
                    log.error(f"Spoolman push error on job {job.id}: {err}")
                appended = "\n".join(f"Spoolman push failed: {e}" for e in push_errors)
                job.notes = f"{job.notes or ''}\n{appended}".strip()
                db.commit()
                db.refresh(job)
        except Exception as e:
            # Helper itself raised; don't let it take down the completion.
            log.error(f"Spoolman push helper raised on job {job.id}: {e}")
            job.notes = f"{job.notes or ''}\nSpoolman push helper error: {e}".strip()
            db.commit()
            db.refresh(job)

    return job


@router.post("/{job_id}/fail", response_model=JobResponse, tags=["Jobs"])
def fail_job(job_id: int, notes: Optional[str] = None, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Mark a job as failed."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.FAILED
    job.actual_end = datetime.now(timezone.utc)
    job.is_locked = True
    if notes:
        job.notes = f"{job.notes or ''}\nFailed: {notes}".strip()

    log_audit(db, "job.failed", "job", job.id, {"printer_id": job.printer_id, "notes": notes})
    db.commit()
    db.refresh(job)
    return job


@router.post("/{job_id}/cancel", response_model=JobResponse, tags=["Jobs"])
def cancel_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Cancel a pending or scheduled job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in [JobStatus.PENDING, JobStatus.SCHEDULED]:
        raise HTTPException(status_code=400, detail="Can only cancel pending or scheduled jobs")
    job.status = JobStatus.CANCELLED
    job.is_locked = True
    log_audit(db, "job.cancelled", "job", job.id)
    db.commit()
    db.refresh(job)
    return job


@router.post("/{job_id}/reset", response_model=JobResponse, tags=["Jobs"])
def reset_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Reset a job back to pending status."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.PENDING
    job.printer_id = None
    job.scheduled_start = None
    job.scheduled_end = None
    job.actual_start = None
    job.actual_end = None
    job.match_score = None
    job.is_locked = False

    log_audit(db, "job.reset", "job", job.id)
    db.commit()
    db.refresh(job)
    return job


@router.post("/{job_id}/dispatch", tags=["Jobs"])
def dispatch_job_to_printer(
    job_id: int,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Dispatch a scheduled Bambu job: upload .3mf via FTPS then start print via MQTT.

    Requires the job to have a linked model with a stored .3mf file on disk.
    The assigned printer must be a Bambu printer with credentials configured.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    if not job.printer_id:
        raise HTTPException(status_code=400, detail="Job is not assigned to a printer")

    try:
        from modules.printers.dispatch import dispatch_job
        success, message = dispatch_job(job.printer_id, job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Dispatch error: {e}")

    if not success:
        raise HTTPException(status_code=400, detail=message)

    db.refresh(job)
    log_audit(db, "job.dispatched", "job", job.id, {"printer_id": job.printer_id})
    db.commit()
    return {"success": True, "message": message, "job_id": job_id, "status": job.status.value}


@router.post("/{job_id}/approve", tags=["Jobs"])
def approve_job(job_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_role("operator"))):
    require_feature("job_approval")
    """Approve a submitted job. Moves it to pending status for scheduling."""

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "submitted":
        raise HTTPException(status_code=400, detail="Job is not in submitted status")

    job.status = JobStatus.PENDING
    job.approved_by = current_user["id"]
    job.approved_at = datetime.now(timezone.utc)
    log_audit(db, "job.approved", "job", job.id, {"approved_by": current_user["id"]})
    db.commit()
    db.refresh(job)

    # Notify the student who submitted
    if job.submitted_by:
        try:
            from modules.notifications.alert_dispatcher import dispatch_alert
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


@router.post("/{job_id}/reject", tags=["Jobs"])
def reject_job(job_id: int, body: _RejectJobRequest, db: Session = Depends(get_db), current_user: dict = Depends(require_role("operator"))):
    require_feature("job_approval")
    """Reject a submitted job with a required reason."""

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "submitted":
        raise HTTPException(status_code=400, detail="Job is not in submitted status")

    if not body.reason or not body.reason.strip():
        raise HTTPException(status_code=400, detail="Rejection reason is required")

    job.status = "rejected"
    job.approved_by = current_user["id"]
    job.rejected_reason = body.reason.strip()
    log_audit(db, "job.rejected", "job", job.id, {"rejected_by": current_user["id"], "reason": body.reason.strip()})
    db.commit()
    db.refresh(job)

    # Notify the student who submitted
    if job.submitted_by:
        try:
            from modules.notifications.alert_dispatcher import dispatch_alert
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


@router.post("/{job_id}/resubmit", tags=["Jobs"])
def resubmit_job(job_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    require_feature("job_approval")
    """Resubmit a rejected job for approval again."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
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
        from modules.notifications.alert_dispatcher import dispatch_alert, get_group_owner_id, get_operator_admin_ids
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


@router.post("/{job_id}/link-print", tags=["Jobs"])
def link_job_to_print(job_id: int, print_job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Link a scheduled job to an MQTT-detected print."""
    # Check job exists and caller has org access
    job_row = db.execute(text("SELECT id, printer_id, charged_to_org_id FROM jobs WHERE id = :id"), {"id": job_id}).fetchone()
    if not job_row:
        raise HTTPException(status_code=404, detail="Job not found")
    job_dict = dict(job_row._mapping)
    if not check_org_access(current_user, job_dict.get("charged_to_org_id")):
        raise HTTPException(status_code=404, detail="Job not found")
    # Re-alias for downstream use
    job = job_row

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


@router.patch("/{job_id}/move", tags=["Jobs"])
def move_job(
    job_id: int,
    request: MoveJobRequest,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Move a job to a different printer and/or time slot."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
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


@router.patch("/{job_id}/failure", tags=["Jobs"])
async def update_job_failure(
    job_id: int,
    request: Request,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db)
):
    """Add or update failure reason and notes on a failed job."""
    data = await request.json()

    job = db.execute(text("SELECT id, status, charged_to_org_id FROM jobs WHERE id = :id"), {"id": job_id}).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job_dict = dict(job._mapping)
    if not check_org_access(current_user, job_dict.get("charged_to_org_id")):
        raise HTTPException(status_code=404, detail="Job not found")
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
        updates.append(f"updated_at = {sql.now()}")
        db.execute(text(f"UPDATE jobs SET {', '.join(updates)} WHERE id = :id"), params)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        db.commit()

    return {"success": True, "message": "Failure info updated"}
