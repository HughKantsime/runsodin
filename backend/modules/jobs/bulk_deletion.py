"""Bulk deletion after the route has acquired authorization and write locks."""
from sqlalchemy import text
from modules.jobs.write_access import bulk_job_writable
from modules.jobs.models import Job
from modules.jobs.services import deleting_job_links
from core.controlled_deletion import controlled_job_ids, redact_job_history_ids


def delete_bulk_jobs(db, current_user, job_ids, skipped, *, check):
    restricted = controlled_job_ids(db, job_ids)
    count = 0
    removed = set()
    for jid in job_ids:
        query = db.query(Job).filter(Job.id == jid)
        job = query.first()
        if not bulk_job_writable(current_user, job, skipped):
            continue
        check(db, jid)
        from modules.orders.services import assert_job_history_deletable
        assert_job_history_deletable(db, jid)
        # Recheck ownership and eligibility after the history check locks the row.
        job = query.populate_existing().first()
        if not bulk_job_writable(current_user, job, skipped):
            continue
        from core.traceability_delivery import prepare_job_deletion
        if job.status not in ('pending', 'submitted', 'cancelled'):
            continue
        prepare_job_deletion(db, jid)
        with deleting_job_links(db, [jid]):
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
