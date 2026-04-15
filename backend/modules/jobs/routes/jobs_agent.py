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
from core.db import get_db
from core.dependencies import log_audit
from core.errors import ErrorCode, OdinError
from core.middleware.dry_run import dry_run_preview, is_dry_run
from core.rbac import (
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
    current_user: dict = Depends(require_role("operator")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
    db: Session = Depends(get_db),
):
    """Cancel a pending or scheduled job. Agent-surface v1.9.0."""
    job = _get_job_or_raise(db, job_id, current_user)
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

    job.status = JobStatus.CANCELLED
    job.is_locked = True
    log_audit(db, "job.cancelled", "job", job.id)
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
    current_user: dict = Depends(require_role("operator")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
):
    """Approve a submitted job. Moves it to pending status. Agent-surface v1.9.0."""
    require_feature("job_approval")
    job = _get_job_or_raise(db, job_id, current_user)

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

    job.status = JobStatus.PENDING
    job.approved_by = current_user["id"]
    job.approved_at = datetime.now(timezone.utc)
    log_audit(db, "job.approved", "job", job.id, {"approved_by": current_user["id"]})
    db.commit()
    db.refresh(job)

    # Notify submitter
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

    return {
        "status": "approved",
        "job_id": job.id,
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
    current_user: dict = Depends(require_role("operator")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
):
    """Reject a submitted job with a required reason. Agent-surface v1.9.0."""
    require_feature("job_approval")
    job = _get_job_or_raise(db, job_id, current_user)

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

    job.status = "rejected"
    job.approved_by = current_user["id"]
    job.rejected_reason = reason
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
