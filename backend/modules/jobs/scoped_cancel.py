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
        'SELECT 1 FROM dispatch_attempt_receipts WHERE job_id=:id',
        'SELECT 1 FROM printer_dispatch_claims WHERE job_id=:id',
        'SELECT 1 FROM print_jobs WHERE scheduled_job_id=:id',
        'SELECT 1 FROM dispatch_sender_registrations WHERE job_id=:id',
    ):
        if db.execute(text(statement), {'id':job.id}).first():
            raise OdinError(ErrorCode.permission_denied,
                            'Dispatch or observed print evidence exceeds write:jobs cancellation authority', status=403)
