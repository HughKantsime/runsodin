"""Shared delivery gate. The caller holds the order lock and owns the commit."""
from fastapi import HTTPException
from sqlalchemy import inspect, text
from core import traceability as service


def assert_inspected_for_delivery(db, job, retained=None):
    from license_manager import get_license
    if not get_license().has_feature('traceability'):
        return retained
    retained = retained or service.record(db, job)
    if retained['inspection_result'] == 'fail' or (service.inspection_required(db, job, retained) and retained['inspection_result'] != 'pass'):
        raise HTTPException(409, 'inspection_required: a passing inspection is required before delivery')
    return retained


def deliver_order(db, order_id, user_id):
    # Order routes already hold the parent lock, shared by inspection/reset.
    from license_manager import get_license
    if not get_license().has_feature('traceability'):
        return
    if not inspect(db.connection()).has_table('traceability_records'):
        return
    jobs = service.rows(db, '''SELECT j.* FROM jobs j WHERE j.status='completed' AND (
        j.order_item_id IN (SELECT id FROM order_items WHERE order_id=:id)
        OR j.id IN (SELECT b.job_id FROM order_production_jobs b
            JOIN order_production_items i ON i.order_item_id=b.order_item_id WHERE i.order_id=:id))
        ORDER BY j.id''', id=order_id)
    for owner in sorted({j['charged_to_org_id'] or 0 for j in jobs}):
        service.lock_policy_scope(db, owner)
    for job in jobs:
        db.execute(text('UPDATE jobs SET id=id WHERE id=:id'), {'id':job['id']})
        retained = assert_inspected_for_delivery(db, job)
        if not retained['delivered_at']:
            db.execute(text('''UPDATE traceability_records SET delivered_by=:user,delivered_at=:at
                WHERE job_id=:id AND attempt=:seq'''),
                {'user':user_id,'at':service.now(),'id':job['id'],'seq':retained['attempt']})
            from core.dependencies import log_audit
            log_audit(db, 'traceability.delivered', 'job', job['id'], {'attempt':retained['attempt'], 'order_id':order_id})


def assert_deletable(db, job_id):
    if inspect(db.connection()).has_table('traceability_records'):
        db.execute(text('UPDATE jobs SET id=id WHERE id=:id'), {'id':job_id})
        if service.one(db, 'SELECT job_id FROM traceability_records WHERE job_id=:id AND delivered_at IS NOT NULL', id=job_id):
            raise HTTPException(409, 'Delivered traceability records must be retained')


def prepare_job_deletion(db, job_id):
    """Caller will delete the locked job; discard only its undelivered receipts."""
    assert_deletable(db, job_id)
    if inspect(db.connection()).has_table('traceability_records'):
        db.execute(text('DELETE FROM traceability_records WHERE job_id=:id AND delivered_at IS NULL'), {'id':job_id})


def retention_candidates(db, job_ids):
    """Keep delivered history while allowing unrelated expired jobs to purge."""
    allowed = []
    for job_id in job_ids:
        try:
            prepare_job_deletion(db, job_id)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            continue
        allowed.append(job_id)
    return allowed
