"""Bulk deletion after the route has acquired authorization and write locks."""
from sqlalchemy import text
from core.rbac import check_org_access
from modules.jobs.models import Job
from core.controlled_deletion import controlled_job_ids, redact_job_history_ids


def delete_bulk_jobs(db, current_user, job_ids, *, check):
    restricted = controlled_job_ids(db, job_ids)
    count = 0
    removed = set()
    for jid in job_ids:
        job = db.query(Job).filter(Job.id == jid).first()
        if not job or not check_org_access(current_user, job.charged_to_org_id):
            continue
        check(db, jid)
        from modules.orders.services import assert_job_history_deletable
        assert_job_history_deletable(db, jid)
        from core.traceability_delivery import prepare_job_deletion
        if job.status in ('pending', 'submitted', 'cancelled'):
            prepare_job_deletion(db, jid)
        changed = db.execute(text("DELETE FROM jobs WHERE id = :id AND status IN ('pending','submitted','cancelled')"),
                   {"id": jid})
        if changed.rowcount:
            removed.add(jid)
            from core.print_attempts import clear_job_attempts, sqlalchemy_runner
            clear_job_attempts(sqlalchemy_runner(db), jid)
            # cert-fix-F's hook also drops the monitor print:<print_job_id> quota markers.
            from core.quota import clear_print_consumption_markers
            clear_print_consumption_markers(sqlalchemy_runner(db), jid)
        count += changed.rowcount
    redact_job_history_ids(db, {jid: restricted[jid] for jid in removed if jid in restricted})
    return count
