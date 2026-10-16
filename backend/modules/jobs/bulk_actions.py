"""Bulk job actions other than delete (POST /jobs/bulk-update).

Kept apart so modules/jobs/routes/jobs_crud.py stays under the route-file size limit.
The bulk delete stays in bulk_update_jobs, where the attempt-marker cleanup census
(test_cert_D_job_attempt_cleanup) requires clear_job_attempts beside the DELETE.
"""
from fastapi import HTTPException
from sqlalchemy import text

from core.base import JobStatus
from core.rbac import check_org_access
from modules.jobs.education_guard import assert_generic_job_mutation_allowed
from modules.jobs.models import Job


def apply_bulk_action(db, current_user, action, job_ids, authorized_ids, priority) -> int:
    """Apply reschedule, cancel, set_priority, hold or unhold; return the affected count.
    Parents were locked and access-checked by the route (authorized_ids)."""
    count = 0
    if action == 'reschedule':
        for jid in sorted(authorized_ids):
            # Parents were locked above. Claim creation takes this same job
            # lock before inserting a preparation fence.
            locked = db.execute(text('UPDATE jobs SET status=status WHERE id=:id RETURNING id'), {'id': jid}).first()
            if locked is None:
                continue
            job = db.query(Job).filter(Job.id == jid).populate_existing().first()
            if not job or not check_org_access(current_user, job.charged_to_org_id):
                continue
            assert_generic_job_mutation_allowed(db, jid)
            if job.status not in (JobStatus.PENDING, JobStatus.SCHEDULED):
                continue
            if db.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE job_id=:id'), {'id': jid}).first():
                continue
            if (job.status == JobStatus.PENDING and job.printer_id is None
                    and job.scheduled_start is None and job.scheduled_end is None
                    and job.match_score is None and not job.is_locked):
                continue
            from modules.jobs.state_machine import transition_job
            count += int(transition_job(db, jid, JobStatus.PENDING, "scheduler", extra_fields={
                "printer_id": None, "scheduled_start": None, "scheduled_end": None,
                "match_score": None, "is_locked": False},
                where="NOT EXISTS(SELECT 1 FROM printer_dispatch_claims WHERE job_id=:claim_job)",
                params={"claim_job": jid}))
    elif action == "cancel":
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if not job or not check_org_access(current_user, job.charged_to_org_id):
                continue
            assert_generic_job_mutation_allowed(db, jid)
            from modules.jobs.state_machine import transition_job
            count += int(transition_job(db, jid, JobStatus.CANCELLED, "operator",
                                        from_states=(JobStatus.PENDING, JobStatus.SUBMITTED)))
    elif action == "set_priority":
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if not job or not check_org_access(current_user, job.charged_to_org_id):
                continue
            assert_generic_job_mutation_allowed(db, jid)
            changed = db.execute(text("UPDATE jobs SET priority = :p WHERE id = :id AND (priority IS NULL OR priority != :p)"), {"p": priority, "id": jid})
            count += changed.rowcount
    elif action == "hold":
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if not job or not check_org_access(current_user, job.charged_to_org_id):
                continue
            assert_generic_job_mutation_allowed(db, jid)
            changed = db.execute(text("UPDATE jobs SET hold = TRUE WHERE id = :id AND hold IS NOT TRUE"), {"id": jid})
            count += changed.rowcount
    elif action == "unhold":
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if not job or not check_org_access(current_user, job.charged_to_org_id):
                continue
            assert_generic_job_mutation_allowed(db, jid)
            changed = db.execute(text("UPDATE jobs SET hold = FALSE WHERE id = :id AND hold IS NOT FALSE"), {"id": jid})
            count += changed.rowcount
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
    return count
