"""Job lifecycle, approval workflow, dispatch, and print-job tracking."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel as PydanticBaseModel
import json
import logging

from modules.jobs.education_guard import assert_generic_job_mutation_allowed
from core.order_production import prepare_job_write, reconcile_job_write
from core.db import get_db
from core.db_compat import sql
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, require_action, check_org_access
from core.base import JobStatus, AlertType, AlertSeverity
from modules.jobs.models import Job
from modules.printers.models import Printer
from modules.inventory.models import Spool
from modules.models_library.models import Model
from modules.jobs.schemas import JobResponse
from modules.jobs.state_machine import transition_job, Expr
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


def _status_value(value):
    return value.value if hasattr(value, "value") else value


class MoveJobRequest(PydanticBaseModel):
    printer_id: int
    scheduled_start: datetime


# _RejectJobRequest moved to jobs_agent.py alongside reject_job.


@router.post("/{job_id}/start", response_model=JobResponse, tags=["Jobs"])
def start_job(job_id: int, current_user: dict = Depends(require_action("jobs.start", scope="write:jobs")), db: Session = Depends(get_db)):
    """Mark a job as started (printing)."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    assert_generic_job_mutation_allowed(db, job_id)
    production_orders = prepare_job_write(db, [job_id])
    job = db.query(Job).filter(Job.id == job_id).populate_existing().first()
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(404, 'Job not found')

    if not transition_job(db, job_id, JobStatus.PRINTING, "operator",
                          extra_fields={"actual_start": datetime.now(timezone.utc), "is_locked": True}):
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Cannot start job in {_status_value(job.status)} status")
    db.refresh(job)

    log_audit(db, "job.started", "job", job.id, {"printer_id": job.printer_id})
    reconcile_job_write(db, production_orders)
    db.commit()
    db.refresh(job)
    return job


@router.post("/{job_id}/complete", response_model=JobResponse, tags=["Jobs"])
def complete_job(job_id: int, current_user: dict = Depends(require_action("jobs.complete", scope="write:jobs")), db: Session = Depends(get_db)):
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
    assert_generic_job_mutation_allowed(db, job_id)
    production_orders = prepare_job_write(db, [job_id])
    job = db.query(Job).filter(Job.id == job_id).populate_existing().first()
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    # R5: atomic state transition through the job state machine. The UPDATE's
    # WHERE clause (allowed from-states) is the lock: one caller wins, a retry
    # of an already-completed job is a no-op, and cancelled/submitted/rejected
    # jobs are refused.
    now = datetime.now(timezone.utc)
    # Decision: a legacy row with NULL status may still be completed (as before
    # the state machine); no other transition accepts NULL.
    applied = transition_job(db, job_id, JobStatus.COMPLETED, "operator",
                             extra_fields={"actual_end": now, "is_locked": True}, include_null=True)
    if not applied:
        current = _status_value(db.execute(
            text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}
        ).scalar())
        if current != JobStatus.COMPLETED.value:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Cannot complete a job in {current} status",
            )
        # Already completed by another request (retry, concurrent operator,
        # or whatever). Return the job as-is with no side effects.
        reconcile_job_write(db, production_orders)
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

    # Apply deductions to spools loaded on this printer through the filament
    # ledger: at most once per print attempt (a failed->completed correction
    # stays on the same attempt; a reset reprint deducts again), and the attempt
    # is claimed only when something is actually deducted.
    if slot_grams and job.printer_id:
        from core.base import SpoolStatus
        from core.filament_ledger import record_job_filament
        from core.print_attempts import sqlalchemy_runner as _attempt_runner
        loaded_spools = db.query(Spool).filter(
            Spool.location_printer_id == job.printer_id,
            Spool.status == SpoolStatus.ACTIVE
        ).all()
        spool_by_slot = {s.location_slot: s for s in loaded_spools if s.location_slot}
        grams_by_spool, slot_of = {}, {}
        for slot_num, grams in slot_grams.items():
            spool = spool_by_slot.get(slot_num)
            if spool is not None and grams > 0:
                grams_by_spool[spool.id] = grams_by_spool.get(spool.id, 0.0) + grams
                slot_of.setdefault(spool.id, slot_num)
        outcome = record_job_filament(_attempt_runner(db), job.id, source="operator_complete",
                                      grams_by_spool=grams_by_spool,
                                      notes=f"job #{job.id} complete ({job.item_name})")
        for spool in loaded_spools:
            db.expire(spool)  # the ledger wrote the balances with SQL
        for entry in outcome.deductions:
            deductions.append({
                "spool_id": entry["spool_id"],
                "slot": slot_of.get(entry["spool_id"]),
                "deducted_g": round(entry["grams"], 1),
                "remaining_g": round(entry["remaining_g"], 1),
                # v1.8.5: carried through to Spoolman push-back below.
                # None for spools not linked to an external Spoolman row.
                "spoolman_spool_id": entry["spoolman_spool_id"],
                "grams": float(entry["grams"]),
                "job_id": job.id,
            })

    if deductions:
        deduct_summary = "; ".join(
            f"Slot {d['slot']}: -{d['deducted_g']}g (spool #{d['spool_id']})"
            for d in deductions
        )
        job.notes = f"{job.notes or ''}\nFilament deducted: {deduct_summary}".strip()

    # Charge the print's material and time to its owner's grams/hours quota
    # (same transaction as the completion).
    from core.quota import record_print_consumption, sqlalchemy_runner
    db.flush()
    record_print_consumption(sqlalchemy_runner(db), job.id, grams=sum(slot_grams.values()))

    log_audit(db, "job.completed", "job", job.id, {"printer_id": job.printer_id, "deductions": len(deductions)})
    reconcile_job_write(db, production_orders)
    db.commit()
    db.refresh(job)

    # v1.8.5: push consumption back to Spoolman for any deductions whose
    # spool has a spoolman_spool_id. Fires AFTER the local commit so the
    # authoritative spool state is persisted regardless of whether
    # Spoolman is reachable.
    #
    # Codex pass 4 (2026-04-14): two changes vs the original v1.8.5 wiring:
    #   1. Background-thread the push so a slow / unreachable Spoolman
    #      can't hold a request worker for 5s × linked_spools per
    #      completion. Slow-Spoolman would otherwise be a worker-
    #      exhaustion DoS surface (an attacker can't directly exploit
    #      it, but a misconfigured Spoolman would degrade the whole
    #      farm). Same daemon-thread pattern as send_webhook in
    #      channels.py.
    #   2. Sanitize the operator-visible note. The previous version
    #      copied the raw exception (which can include the configured
    #      Spoolman URL or an internal IP from WebhookSSRFError) into
    #      job.notes, which is returned in JobResponse and visible to
    #      every viewer with job access. The note now says only that
    #      a push failed and points at server logs.
    if deductions:
        from modules.inventory.services import push_consumption_to_spoolman
        import threading

        def _push_async(snapshot_deductions, snapshot_job_id):
            # New session for the background thread — never share a
            # request-bound Session across threads.
            from core.db import SessionLocal
            try:
                push_errors = push_consumption_to_spoolman(snapshot_deductions)
                if push_errors:
                    for err in push_errors:
                        log.error(f"Spoolman push error on job {snapshot_job_id}: {err}")
                    bg = SessionLocal()
                    try:
                        # Sanitized, short note — exception detail stays in
                        # the server log, never the customer-visible field.
                        marker = (
                            f"\n[v1.8.6] Spoolman push failed for "
                            f"{len(push_errors)} spool(s); see server logs."
                        )
                        bg.execute(
                            text("UPDATE jobs SET notes = COALESCE(notes,'') || :m WHERE id = :id"),
                            {"m": marker, "id": snapshot_job_id},
                        )
                        bg.commit()
                    finally:
                        bg.close()
            except Exception as e:
                # Helper itself raised in the background. Log only —
                # do NOT propagate the exception text to job.notes.
                log.error(f"Spoolman push helper raised on job {snapshot_job_id}: {e}")
                bg = SessionLocal()
                try:
                    bg.execute(
                        text("UPDATE jobs SET notes = COALESCE(notes,'') || :m WHERE id = :id"),
                        {"m": "\n[v1.8.6] Spoolman push helper error; see server logs.",
                         "id": snapshot_job_id},
                    )
                    bg.commit()
                finally:
                    bg.close()

        threading.Thread(
            target=_push_async,
            args=(list(deductions), job.id),
            daemon=True,
        ).start()

    return job


@router.post("/{job_id}/fail", response_model=JobResponse, tags=["Jobs"])
def fail_job(job_id: int, notes: Optional[str] = None, current_user: dict = Depends(require_action("jobs.complete", scope="write:jobs")), db: Session = Depends(get_db)):
    """Mark a job as failed."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    assert_generic_job_mutation_allowed(db, job_id)
    production_orders = prepare_job_write(db, [job_id])
    job = db.query(Job).filter(Job.id == job_id).populate_existing().first()
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    # Atomic transition through the state machine: the UPDATE predicate (not a
    # Python read) decides, so a concurrent cancel/reject cannot be overwritten.
    failed_notes = f"{job.notes or ''}\nFailed: {notes}".strip() if notes else job.notes
    if not transition_job(db, job_id, JobStatus.FAILED, "operator",
                          extra_fields={"actual_end": datetime.now(timezone.utc), "is_locked": True,
                                        "notes": failed_notes}):
        current = db.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}).scalar()
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Cannot fail a job in {_status_value(current)} status",
        )
    db.refresh(job)

    log_audit(db, "job.failed", "job", job.id, {"printer_id": job.printer_id, "notes": notes})
    reconcile_job_write(db, production_orders)
    db.commit()
    db.refresh(job)
    return job


# cancel_job moved to jobs_agent.py in v1.9.0 Phase 2 (architecture cap).


@router.post("/{job_id}/reset", response_model=JobResponse, tags=["Jobs"])
def reset_job(job_id: int, current_user: dict = Depends(require_action("jobs.edit", scope="write:jobs")), db: Session = Depends(get_db)):
    """Reset a job back to pending status."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    assert_generic_job_mutation_allowed(db, job_id)
    production_orders = prepare_job_write(db, [job_id])
    job = db.query(Job).filter(Job.id == job_id).populate_existing().first()
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    # Reset starts a new print attempt and is only allowed from a finished
    # outcome (completed/failed/cancelled) - never from submitted, rejected or
    # a live print. The explicit pin (target columns) is kept.
    if not transition_job(db, job_id, JobStatus.PENDING, "operator", extra_fields={
            "printer_id": None, "scheduled_start": None, "scheduled_end": None,
            "actual_start": None, "actual_end": None, "match_score": None,
            "is_locked": False, "queue_position": None}):
        db.rollback()
        raise HTTPException(status_code=409,
                            detail=f"Cannot reset a job in {_status_value(job.status)} status")
    from core.print_attempts import bump_print_attempt, sqlalchemy_runner
    bump_print_attempt(sqlalchemy_runner(db), job_id)
    db.refresh(job)

    # One shared counter (MERGE_NOTES_F): D's bump above also starts the next quota attempt.
    log_audit(db, "job.reset", "job", job.id)
    reconcile_job_write(db, production_orders)
    db.commit()
    db.refresh(job)
    return job


@router.post("/{job_id}/dispatch", tags=["Jobs"])
def dispatch_job_to_printer(
    job_id: int,
    current_user: dict = Depends(require_action("jobs.start", scope="write:jobs")),
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


# approve_job + reject_job moved to jobs_agent.py in v1.9.0 Phase 2
# (architecture cap — see conductor/tracks/agent-native-phase2-safety_20260415/).


@router.post("/{job_id}/resubmit", tags=["Jobs"])
def resubmit_job(job_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_action("jobs.resubmit", scope="write:jobs"))):
    require_feature("job_approval")
    """Resubmit a rejected job for approval again."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    assert_generic_job_mutation_allowed(db, job_id)

    if job.status != "rejected":
        raise HTTPException(status_code=400, detail="Only rejected jobs can be resubmitted")

    if job.submitted_by != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the original submitter can resubmit")

    if not transition_job(db, job_id, JobStatus.SUBMITTED, "approval", extra_fields={
            "rejected_reason": None, "approved_by": None, "approved_at": None},
            where="submitted_by = :submitter", params={"submitter": current_user["id"]}):
        db.rollback()
        raise HTTPException(status_code=409, detail="Job changed; reload before resubmitting")
    db.commit()
    db.refresh(job)

    # Re-notify the group owner (or the group reviewers as fallback)
    try:
        from modules.notifications.alert_dispatcher import dispatch_alert, get_job_approval_reviewer_ids
        target_ids = get_job_approval_reviewer_ids(db, current_user)
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
def link_job_to_print(job_id: int, print_job_id: int, current_user: dict = Depends(require_action("jobs.edit", scope="write:jobs")), db: Session = Depends(get_db)):
    """Link a scheduled job to an MQTT-detected print."""
    # Check job exists and caller has org access
    job_row = db.execute(text("SELECT id, printer_id, charged_to_org_id FROM jobs WHERE id = :id"), {"id": job_id}).fetchone()
    if not job_row:
        raise HTTPException(status_code=404, detail="Job not found")
    job_dict = dict(job_row._mapping)
    if not check_org_access(current_user, job_dict.get("charged_to_org_id")):
        raise HTTPException(status_code=404, detail="Job not found")
    assert_generic_job_mutation_allowed(db, job_id)
    production_orders = prepare_job_write(db, [job_id])
    job_row = db.execute(text("SELECT id,printer_id,charged_to_org_id FROM jobs WHERE id=:id"), {'id': job_id}).fetchone()
    if not job_row or not check_org_access(current_user, job_row.charged_to_org_id):
        raise HTTPException(404, 'Job not found')
    # Re-alias for downstream use
    job = job_row

    # Check print_job exists
    print_job = db.execute(text("SELECT id, printer_id, status, scheduled_job_id FROM print_jobs WHERE id = :id"), {"id": print_job_id}).fetchone()
    if not print_job:
        raise HTTPException(status_code=404, detail="Print job not found")

    # Verify same printer
    if job.printer_id is None or job.printer_id != print_job.printer_id:
        raise HTTPException(status_code=400, detail="Job and print are on different printers")

    printer = db.query(Printer).filter(Printer.id == job.printer_id).first()
    if not printer or (not printer.shared and not check_org_access(current_user, printer.org_id)):
        raise HTTPException(404, 'Print job not found')
    if print_job.scheduled_job_id not in (None, job_id):
        raise HTTPException(409, 'This print is already linked to another job')

    # A print belongs to the attempt in which it started. One that started before
    # the job was last reset belongs to an earlier attempt: linking it now would
    # account it to the current attempt, so refuse instead of mis-binding it.
    from core.filament_ledger import current_attempt, print_attempt
    from core.print_attempts import bind_print_attempt, sqlalchemy_runner
    attempt_run = sqlalchemy_runner(db)
    own_attempt = print_attempt(attempt_run, job_id, print_job_id)
    if print_job.scheduled_job_id is None and not own_attempt.same_attempt(current_attempt(attempt_run, job_id)):
        raise HTTPException(409, "This print started before the job was last reset; it belongs to an earlier "
                                 "attempt and cannot be linked to the current one")

    # Link them
    linked = db.execute(text("UPDATE print_jobs SET scheduled_job_id = :job_id WHERE id = :id AND (scheduled_job_id IS NULL OR scheduled_job_id=:job_id) RETURNING status"),
               {"job_id": job_id, "id": print_job_id})

    locked_print = linked.fetchone()
    if locked_print is None:
        raise HTTPException(409, "Print linkage changed before this request")
    bind_print_attempt(attempt_run, print_job_id, job_id, seq=own_attempt.seq)

    # Update job status based on print status
    new_status = None
    if locked_print.status == 'completed':
        new_status = 'completed'
    elif locked_print.status == 'failed':
        new_status = 'failed'
    elif locked_print.status == 'cancelled':
        new_status = 'cancelled'
    elif locked_print.status in ('printing', 'running'):
        new_status = 'printing'

    # Through the state machine as an observed print: never moves a
    # cancelled/submitted/rejected job and never overwrites a terminal status.
    status_applied = None
    if new_status:
        status_applied = transition_job(db, job_id, new_status, "monitor")

    reconcile_job_write(db, production_orders)
    db.commit()
    return {"message": "Linked", "job_id": job_id, "print_job_id": print_job_id,
            "status_applied": bool(status_applied)}


@router.patch("/{job_id}/move", tags=["Jobs"])
def move_job(
    job_id: int,
    request: MoveJobRequest,
    current_user: dict = Depends(require_action("timeline.move", scope="write:jobs")), db: Session = Depends(get_db)
):
    """Move a job to a different printer and/or time slot (RBAC action timeline.move)."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    assert_generic_job_mutation_allowed(db, job_id)
    production_orders = prepare_job_write(db, [job_id])
    job = db.query(Job).filter(Job.id == job_id).populate_existing().first()
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(404, 'Job not found')

    if job.status not in (JobStatus.PENDING, JobStatus.SCHEDULED):
        raise HTTPException(409, 'Only pending or scheduled jobs can be moved; reset terminal work explicitly')

    # Verify printer exists
    printer = db.query(Printer).filter(Printer.id == request.printer_id).first()
    if not printer or (not printer.shared and not check_org_access(current_user, printer.org_id)):
        raise HTTPException(status_code=404, detail="Printer not found")

    # Calculate end time based on job duration
    duration_hours = job.effective_duration
    if duration_hours is None:
        raise HTTPException(409, "Set an explicit quantity before manually scheduling this job")
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

    # Moving a job onto a printer is an explicit printer choice: a pinned job's
    # pin follows the move (it never stays pointing at the old printer).
    # A deleted:<id> pin is re-pinned to the printer the operator chose.
    from core.print_file_binding import job_file_id, require_bound_target
    require_bound_target(db, job_file_id(db, job.model_id, job.print_file_id), request.printer_id)
    from modules.jobs.pinning import ISSUE_DELETED, parse_target, pin_columns
    moved = {"printer_id": request.printer_id, "scheduled_start": request.scheduled_start,
             "scheduled_end": scheduled_end}
    pin, pin_problem = parse_target(job.target_type, job.target_filter)
    if pin is not None or pin_problem == ISSUE_DELETED:
        moved.update(pin_columns(request.printer_id))
    if not transition_job(db, job_id, JobStatus.SCHEDULED, "operator", extra_fields=moved, retarget=True):
        db.rollback()
        raise HTTPException(409, 'Only pending or scheduled jobs can be moved; reset terminal work explicitly')
    db.expire(job)

    reconcile_job_write(db, production_orders)
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
    current_user: dict = Depends(require_action("jobs.edit", scope="write:jobs")),
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
    assert_generic_job_mutation_allowed(db, job_id)
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
