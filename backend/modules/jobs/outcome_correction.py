"""Operator metadata correction. No terminal delivery/accounting hooks run here."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, PositiveInt
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_action, check_org_access
from modules.jobs.education_guard import assert_generic_job_mutation_allowed
from modules.jobs.models import Job
from modules.jobs.state_machine import transition_job
from modules.archives.services import assert_archive_access

router = APIRouter()


class OutcomeCorrection(BaseModel):
    from_status: Literal['failed', 'cancelled']
    status: Literal['failed', 'cancelled']
    print_id: PositiveInt | None = None


@router.post('/jobs/{job_id}/outcome', tags=['Jobs'])
def reclassify_outcome(job_id: int, body: OutcomeCorrection,
                       db: Session = Depends(get_db),
                       current_user: dict = Depends(require_action('jobs.edit', scope='write:jobs'))):
    job = db.get(Job, job_id)
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(404, 'Job not found')
    assert_generic_job_mutation_allowed(db, job_id)
    if body.status == body.from_status:
        raise HTTPException(400, 'Only failed ↔ cancelled corrections are allowed')
    # The state-machine UPDATE locks the job and guards concurrent reset/retry.
    owner_guard = 'charged_to_org_id IS NULL' if job.charged_to_org_id is None else 'charged_to_org_id=:correction_org'
    if not transition_job(db, job_id, body.status, 'outcome_correction', from_states=[body.from_status],
                          where=owner_guard, params={'correction_org': job.charged_to_org_id}):
        db.rollback()
        raise HTTPException(409, 'Job outcome changed or is not failed/cancelled')
    attempt = db.execute(text('SELECT id,status FROM print_jobs WHERE scheduled_job_id=:job ORDER BY id DESC LIMIT 1'),
                         {'job': job_id}).first()
    if (not attempt or attempt.status != body.from_status
            or (body.print_id is not None and body.print_id != attempt.id)):
        db.rollback()
        raise HTTPException(409, 'The latest print must have the same terminal outcome as its job')
    # Historical archive ownership can differ from a job's current owner.
    # Reuse the archive's own audience guard before changing any history.
    try:
        for archive_id in db.execute(text('SELECT id FROM print_archives WHERE print_job_id=:id'), {'id': attempt.id}).scalars():
            assert_archive_access(db, archive_id, current_user)
    except HTTPException:
        db.rollback()
        raise
    changed = db.execute(text('UPDATE print_jobs SET status=:target WHERE id=:id AND status=:source AND scheduled_job_id=:job'),
                         {'target': body.status, 'source': body.from_status, 'id': attempt.id, 'job': job_id})
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, 'Print outcome changed; reload before correcting')
    db.execute(text('UPDATE print_archives SET status=:target WHERE print_job_id=:id AND status=:source'),
               {'target': body.status, 'source': body.from_status, 'id': attempt.id})
    log_audit(db, 'job.outcome_reclassified', 'job', job_id,
              {'from': body.from_status, 'to': body.status, 'print_id': attempt.id})
    db.commit()
    return {'job_id': job_id, 'print_id': attempt.id, 'status': body.status}
