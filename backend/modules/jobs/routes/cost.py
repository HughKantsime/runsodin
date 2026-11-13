"""What a finished job cost to print, next to its estimate. Free on every tier, like the estimate."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import AGENT_READ_SCOPE, AGENT_WRITE_SCOPE, check_org_access, require_any_scope, require_role
from modules.jobs.models import Job

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.get("/{job_id}/cost")
def get_job_cost(
    job_id: int,
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE, "read:jobs")),
    db: Session = Depends(get_db),
):
    """Estimate and actual cost of one job. Visible exactly when GET /jobs/{job_id} is.

    `configured` false (no saved pricing settings) or `finished` false: no amounts are returned.
    state 'none': the job has no recorded print attempts, so there is no actual cost.
    state 'partial': `total` is a lower bound ("at least"); `unknown` says what is missing.
    Machine and from-settings lines use today's pricing settings, not the rates on the print day.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    from core.controlled_parts import assert_access
    assert_access(db, "job", job_id, current_user)
    from modules.jobs.cost_actual import job_cost
    return job_cost(db, job, current_user)
