"""GDPR projection and caller-owned erasure for E4 records (no license gate)."""
from sqlalchemy import inspect, text
from core.identity_scrub import scrub_json_text, scrub_text


def export_personal(db, user_id):
    if not inspect(db.connection()).has_table('traceability_records'):
        return []
    return [dict(r._mapping) for r in db.execute(text('''SELECT r.job_id,r.attempt,r.snapshot,r.captured_at,r.inspection_result,
        CASE WHEN r.inspected_by=:uid THEN r.inspection_notes ELSE NULL END AS inspection_notes,r.inspected_at,r.delivered_at FROM traceability_records r
        JOIN jobs j ON j.id=r.job_id WHERE r.inspected_by=:uid OR r.delivered_by=:uid
        OR j.submitted_by=:uid OR j.charged_to_user_id=:uid ORDER BY r.job_id,r.attempt'''), {'uid':user_id})]


def erase_personal(db, user_id, identifiers):
    if not inspect(db.connection()).has_table('traceability_records'):
        return
    db.execute(text('''UPDATE traceability_records SET inspected_by=NULL
        WHERE inspected_by=:uid'''), {'uid':user_id})
    db.execute(text('UPDATE traceability_records SET delivered_by=NULL WHERE delivered_by=:uid'), {'uid':user_id})
    from core.traceability import decoded, snapshot_bytes, snapshot_digest
    from core.dependencies import log_audit
    for row in db.execute(text('SELECT job_id,attempt,snapshot,inspection_notes FROM traceability_records')):
        snapshot = scrub_json_text(row.snapshot, identifiers) or row.snapshot
        saved = decoded(snapshot, {})
        saved['operator_ids'] = [uid for uid in saved.get('operator_ids', []) if uid != user_id]
        snapshot = snapshot_bytes(saved)
        notes = scrub_text(row.inspection_notes, identifiers)
        if snapshot != row.snapshot or notes != row.inspection_notes:
            db.execute(text('''UPDATE traceability_records SET snapshot=:snapshot,snapshot_sha256=:digest,inspection_notes=:notes
                WHERE job_id=:id AND attempt=:seq'''), {'id':row.job_id,'seq':row.attempt,'snapshot':snapshot,'digest':snapshot_digest(saved),'notes':notes})

            log_audit(db, 'traceability.erased', 'job', row.job_id, {'attempt':row.attempt, 'snapshot_sha256':snapshot_digest(saved)})
