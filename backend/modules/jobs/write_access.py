"""Apply the shared organization write rule using the job's recorded submitter."""
from fastapi import HTTPException

from core.rbac import check_org_write, require_org_write, unscoped_write_detail


def is_job_submitter(actor, submitted_by):
    return bool(actor) and submitted_by is not None and submitted_by == actor.get('id')


def can_write_job(actor, job):
    return check_org_write(actor, job.charged_to_org_id, operate=is_job_submitter(actor, job.submitted_by))


def require_job_write(actor, job):
    if job is None:
        raise HTTPException(404, 'Job not found')
    require_org_write(actor, job.charged_to_org_id, 'job', operate=is_job_submitter(actor, job.submitted_by))


def bulk_job_writable(actor, job, skipped):
    if job is None:
        return False
    if can_write_job(actor, job):
        return True
    if actor and job.charged_to_org_id is None:
        skipped[job.id] = {'job_id': job.id, 'detail': unscoped_write_detail('job')}
    return False
