"""The write:jobs cancellation exception never authorizes printer actions."""
from sqlalchemy import text
from core.errors import ErrorCode, OdinError
from core.rbac import AGENT_WRITE_SCOPE, is_scoped_token, token_scope_granted
from modules.jobs.education_guard import assert_generic_job_mutation_allowed


def assert_cancel_allowed(db, job, user):
    assert_generic_job_mutation_allowed(db, job.id)
    # The route still enforces jobs.cancel. Only the newly admitted write:jobs
    # (or broad write) tokens get narrower rules; preserve existing authority.
    scopes = user.get('_token_scopes') or []
    if not is_scoped_token(user) or 'admin' in scopes or AGENT_WRITE_SCOPE in scopes:
        return
    if not token_scope_granted(scopes, 'write:jobs'):
        raise OdinError(ErrorCode.scope_denied, 'write:jobs scope required', status=403)
    if job.status not in ('pending', 'scheduled') or (job.status != 'pending' and not job.hold):
        raise OdinError(ErrorCode.permission_denied,
                        'Scoped cancel requires a pending or held job that has never been dispatched', status=403)
    for statement in (
        'SELECT 1 FROM printer_dispatch_claims WHERE job_id=:id',
        'SELECT 1 FROM print_jobs WHERE scheduled_job_id=:id',
        'SELECT 1 FROM dispatch_sender_registrations WHERE job_id=:id',
    ):
        if db.execute(text(statement), {'id':job.id}).first():
            raise OdinError(ErrorCode.permission_denied,
                            'Dispatch or observed print evidence exceeds write:jobs cancellation authority', status=403)

    receipts = db.execute(text('SELECT claim_token,created_at,observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'), {'id':job.id}).mappings().all()
    if receipts:
        import json
        from core.db_compat import as_utc_datetime
        audits = db.execute(text("SELECT timestamp,details FROM audit_logs WHERE entity_type='job' AND entity_id=:id AND action='operator.dispatch_no_start_reconciled'"), {'id':job.id}).mappings().all()
        for receipt in receipts:
            reconciled = False
            if receipt['observed_print_job_id'] is None:
                for audit in audits:
                    try:
                        detail = json.loads(audit['details']) if isinstance(audit['details'], str) else audit['details']
                        # An old no-start decision must not clear a later receipt.
                        if (isinstance(detail, dict) and detail.get('job_left_held') is True
                                and detail.get('observed_print_job_id') is None
                                and (len(receipts)==1 or detail.get('claim_token') == receipt['claim_token']
                                     or detail.get('attempt_id') == receipt['claim_token'])
                                and detail.get('claim_token', receipt['claim_token']) == receipt['claim_token']
                                and detail.get('attempt_id', receipt['claim_token']) == receipt['claim_token']
                                and as_utc_datetime(audit['timestamp']) >= as_utc_datetime(receipt['created_at'])):
                            reconciled = True
                    except (TypeError, ValueError):
                        continue
            if not reconciled:
                raise OdinError(ErrorCode.permission_denied,
                                'Dispatch or observed print evidence exceeds write:jobs cancellation authority', status=403)
