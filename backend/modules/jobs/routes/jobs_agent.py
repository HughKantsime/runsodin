"""Agent-surface job lifecycle writes — cancel, approve, reject.

Split from `jobs_lifecycle.py` in v1.9.0 Phase 2 — the retrofit
(agent-scope stacking + dry-run branches + next_actions) pushed the
parent file past the 700-line architecture cap. These three routes
are the ones the MCP v2.0.4 / v2.1.0 tool layer advertises; keeping
them together makes the agent-surface easier to audit and test.

Every route here follows the Phase 2 canonical pattern:
  - Stacked auth: `require_role("operator")` (JWT floor, because
    `require_any_scope` bypasses JWT) + `require_any_scope("admin",
    AGENT_WRITE_SCOPE)` (token-scope gate).
  - `is_dry_run(request)` branch BEFORE any `db.commit` or alert
    dispatch. Returns `dry_run_preview(...)` with a detailed
    `would_execute` shape.
  - `OdinError(ErrorCode.*, status=N)` for all 4xx/5xx — dual-shape
    envelope; MCP clients branch on `error.code`.
  - `next_actions` emitted on success via `build_next_actions` +
    `next_action` helpers.
  - Registered in `core.middleware.dry_run.DRY_RUN_SUPPORTED_ROUTES`.
"""

from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

from core.base import AlertSeverity, AlertType, JobStatus
from modules.jobs.education_guard import assert_generic_job_mutation_allowed
from core.order_production import prepare_job_write, reconcile_job_write
from core.db import get_db
from core.dependencies import log_audit
from core.errors import ErrorCode, OdinError
from core.middleware.dry_run import dry_run_preview, is_dry_run
from core.rbac import (
    require_action,
    AGENT_WRITE_SCOPE,
    check_org_access,
    require_any_scope,
    require_role,
)
from core.responses import build_next_actions, next_action
from license_manager import require_feature
from modules.jobs.models import Job
from modules.jobs.schemas import JobResponse

log = logging.getLogger("odin.api")
logger = log

router = APIRouter(prefix="/jobs", tags=["Jobs"])


class _RejectJobRequest(PydanticBaseModel):
    """Inline schema for reject endpoint."""
    reason: str


def _get_job_or_raise(db: Session, job_id: int, current_user: dict) -> Job:
    """Shared lookup + org-access check. Raises OdinError(job_not_found)."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise OdinError(
            ErrorCode.job_not_found,
            f"Job {job_id} not found",
            status=404,
        )
    return job


@router.post("/{job_id}/cancel", tags=["Jobs"])
def cancel_job(
    job_id: int,
    request: Request,
    current_user: dict = Depends(require_action("jobs.cancel")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
    db: Session = Depends(get_db),
):
    """Cancel a pending or scheduled job. Agent-surface v1.9.0."""
    job = _get_job_or_raise(db, job_id, current_user)
    assert_generic_job_mutation_allowed(db, job_id)
    if job.status not in [JobStatus.PENDING, JobStatus.SCHEDULED]:
        raise OdinError(
            ErrorCode.invalid_state_transition,
            f"Can only cancel pending or scheduled jobs (current: {job.status})",
            status=400,
            retriable=False,
        )

    if is_dry_run(request):
        return dry_run_preview(
            would_execute={
                "action": "cancel_job",
                "job_id": job_id,
                "item_name": job.item_name,
                "from_status": str(job.status),
                "to_status": "cancelled",
                "would_lock": True,
            },
            next_actions=[
                next_action("get_job", {"id": job_id}, "verify cancelled state"),
                next_action("list_queue", {}, "see queue depth"),
            ],
            notes="Would set jobs.status=cancelled and is_locked=True.",
        )

    production_orders = prepare_job_write(db, [job_id])
    job = db.query(Job).filter(Job.id == job_id).populate_existing().first()
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise OdinError(ErrorCode.job_not_found, f"Job {job_id} not found", status=404)
    from modules.jobs.state_machine import transition_job
    if not transition_job(db, job_id, JobStatus.CANCELLED, "operator",
                          from_states=(JobStatus.PENDING, JobStatus.SCHEDULED),
                          extra_fields={"is_locked": True}):
        db.rollback()
        raise OdinError(ErrorCode.invalid_state_transition,
                        'Job state changed before cancellation', status=400, retriable=False)
    db.refresh(job)
    log_audit(db, "job.cancelled", "job", job.id)
    reconcile_job_write(db, production_orders)
    db.commit()
    db.refresh(job)
    response = JobResponse.model_validate(job, from_attributes=True).model_dump(mode="json")
    response["next_actions"] = build_next_actions(
        next_action("get_job", {"id": job_id}, "confirm cancelled state"),
        next_action("list_queue", {}, "see queue depth after cancel"),
    )
    return response


@router.post("/{job_id}/approve", tags=["Jobs"])
def approve_job(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_action("jobs.approve")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
):
    """Approve a submitted job. Moves it to pending status. Agent-surface v1.9.0."""
    require_feature("job_approval")
    job = _get_job_or_raise(db, job_id, current_user)
    assert_generic_job_mutation_allowed(db, job_id)

    if job.status != "submitted":
        raise OdinError(
            ErrorCode.invalid_state_transition,
            f"Job is not in submitted status (current: {job.status})",
            status=400,
            retriable=False,
        )

    if is_dry_run(request):
        return dry_run_preview(
            would_execute={
                "action": "approve_job",
                "job_id": job_id,
                "item_name": job.item_name,
                "from_status": "submitted",
                "to_status": "pending",
                "approver_id": current_user["id"],
                "would_notify_submitter": bool(job.submitted_by),
            },
            next_actions=[
                next_action("get_job", {"id": job_id}, "verify approved state"),
                next_action("list_queue", {}, "see job in queue"),
            ],
            notes="Would set jobs.status=pending + approved_by/at; would dispatch JOB_APPROVED alert to submitter.",
        )

    # Everything the approval needs is read before its first write; the conditional
    # status UPDATE takes SQLite's writer, then the audit and the commit follow at once.
    from modules.jobs.state_machine import transition_job
    submitted_by = job.submitted_by
    if not transition_job(db, job_id, JobStatus.PENDING, "approval", extra_fields={
            "approved_by": current_user["id"], "approved_at": datetime.now(timezone.utc)}):
        db.rollback()
        raise OdinError(ErrorCode.invalid_state_transition, "Job is no longer awaiting approval",
                        status=409, retriable=False)
    # Audit is atomic with the approval it records; so is the alert it owes (one outbox
    # row, fanned out after the commit by modules.notifications.alert_outbox).
    log_audit(db, "job.approved", "job", job.id, {"approved_by": current_user["id"]})
    import modules.notifications.alert_outbox as alert_outbox
    outbox_id = alert_outbox.enqueue(db, job_id, "job_approved", current_user["id"]) if submitted_by else None
    db.commit()
    if outbox_id is not None:
        # A failure here leaves the row for the replay sweep; it never undoes the approval.
        alert_outbox.dispatch(db.get_bind(), outbox_id)

    return {
        "status": "approved",
        "job_id": job_id,
        "next_actions": build_next_actions(
            next_action("get_job", {"id": job_id}, "confirm approved state"),
            next_action("list_queue", {}, "see job moved to queue"),
        ),
    }


@router.post("/{job_id}/reject", tags=["Jobs"])
def reject_job(
    job_id: int,
    body: _RejectJobRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_action("jobs.reject")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
):
    """Reject a submitted job with a required reason. Agent-surface v1.9.0."""
    require_feature("job_approval")
    job = _get_job_or_raise(db, job_id, current_user)
    assert_generic_job_mutation_allowed(db, job_id)

    if job.status != "submitted":
        raise OdinError(
            ErrorCode.invalid_state_transition,
            f"Job is not in submitted status (current: {job.status})",
            status=400,
            retriable=False,
        )

    if not body.reason or not body.reason.strip():
        raise OdinError(
            ErrorCode.validation_failed,
            "Rejection reason is required",
            status=400,
            retriable=False,
        )

    reason = body.reason.strip()

    if is_dry_run(request):
        return dry_run_preview(
            would_execute={
                "action": "reject_job",
                "job_id": job_id,
                "item_name": job.item_name,
                "from_status": "submitted",
                "to_status": "rejected",
                "rejector_id": current_user["id"],
                "reason": reason,
                "would_notify_submitter": bool(job.submitted_by),
            },
            next_actions=[
                next_action("get_job", {"id": job_id}, "verify rejected state"),
                next_action("list_jobs", {"status_filter": "rejected"}, "confirm job in rejected list"),
            ],
            notes="Would set jobs.status=rejected + rejected_reason; would dispatch JOB_REJECTED alert to submitter.",
        )

    from modules.jobs.state_machine import transition_job
    if not transition_job(db, job_id, JobStatus.REJECTED, "approval", extra_fields={
            "approved_by": current_user["id"], "rejected_reason": reason}):
        db.rollback()
        raise OdinError(ErrorCode.invalid_state_transition, "Job is no longer awaiting approval",
                        status=409, retriable=False)
    db.refresh(job)
    log_audit(db, "job.rejected", "job", job.id, {"rejected_by": current_user["id"], "reason": reason})
    db.commit()
    db.refresh(job)

    # Notify submitter
    if job.submitted_by:
        try:
            from modules.notifications.alert_dispatcher import dispatch_alert
            dispatch_alert(
                db=db,
                alert_type=AlertType.JOB_REJECTED,
                severity=AlertSeverity.WARNING,
                title=f"Job rejected: {job.item_name or 'Untitled'}",
                message=f"Reason: {reason}",
                job_id=job.id,
                target_user_ids=[job.submitted_by],
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch job_rejected alert: {e}")

    return {
        "status": "rejected",
        "job_id": job.id,
        "reason": reason,
        "next_actions": build_next_actions(
            next_action("get_job", {"id": job_id}, "confirm rejected state"),
        ),
    }
