"""Interactive platform recovery; all state and audit share the caller transaction."""
from sqlalchemy import text
from core.errors import ErrorCode, OdinError
from core.rbac import is_interactive_session
from core.dependencies import log_audit


def restore_superadmin(db, *, actor, user_id, confirmation):
    from modules.organizations.password_reset import revoke_sessions_and_biometric
    from core.auth import ACCESS_TOKEN_EXPIRE_HOURS
    def refuse(detail, status=409):
        raise OdinError(ErrorCode.resource_conflict if status == 409 else ErrorCode.permission_denied, detail, status=status)
    try:
        if not is_interactive_session(actor) or actor.get("_auth_kind") not in {"session_jwt", "bearer_jwt"}:
            refuse('Interactive session required', 403)
        # Deterministic account lock order also prevents concurrent actor demotion.
        ids = sorted({int(actor['id']), user_id})
        suffix = ' FOR UPDATE' if db.get_bind().dialect.name == 'postgresql' else ''
        accounts = {}
        for uid in ids:
            if not suffix:
                db.execute(text('UPDATE users SET id=id WHERE id=:id'), {'id': uid})
            row = db.execute(text('SELECT id,username,role,group_id,is_active FROM users WHERE id=:id' + suffix), {'id': uid}).mappings().first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — suffix is a fixed dialect-selected FOR UPDATE literal; values are bound
            accounts[uid] = dict(row) if row else None
        live_actor = accounts[int(actor['id'])]
        if not live_actor or live_actor['role'] != 'admin' or live_actor['group_id'] is not None or not live_actor['is_active']:
            refuse('Platform superadmin required', 403)
        target = accounts[user_id]
        if not target:
            raise OdinError(ErrorCode.user_not_found, 'User not found', status=404)
        if target['role'] != 'admin' or target['group_id'] is None:
            refuse('Target must be an organization admin')
        if confirmation != target['username']:
            refuse('Type the target username exactly', 422)
        params = {'id': user_id, 'actor': actor['id'], 'org': target['group_id'], 'actor_text': str(user_id)}
        checks = (
            "SELECT 1 FROM education_submissions s JOIN jobs j ON j.id=s.job_id WHERE (s.submitted_by=:id OR s.approved_by=:id) AND s.status NOT IN ('completed','failed','rejected','cancelled') AND j.status IN ('pending','scheduled','printing') LIMIT 1",
            "SELECT 1 FROM education_upload_operations WHERE user_id=:id AND (state IN ('reserved','streaming','staged','renamed','needs_reconciliation','purge_pending') OR lease_owner IS NOT NULL OR lease_expires_at>CURRENT_TIMESTAMP OR (reserved_bytes>0 AND reservation_released_at IS NULL)) LIMIT 1",
            "SELECT 1 FROM education_monitor_claims c JOIN education_submissions s ON s.id=c.submission_id WHERE s.submitted_by=:id OR s.approved_by=:id LIMIT 1",
            "SELECT 1 FROM education_commands WHERE actor_kind='user' AND actor_id=:actor_text AND state='pending' LIMIT 1",
            "SELECT 1 FROM education_storage_accounts WHERE user_id=:id AND reserved_bytes>0 LIMIT 1",
        )
        # The account lock conflicts with all FK key-share locks on new retained
        # identities; lock dependent tables as well to exclude already-retained writers.
        if suffix:
            db.execute(text('LOCK TABLE education_cost_center_grants, education_submissions, education_upload_operations, education_monitor_claims, education_commands, education_storage_accounts IN SHARE ROW EXCLUSIVE MODE'))
        if any(db.execute(text(query), params).first() for query in checks):
            refuse('Resolve live Education work before restoring this account')
        grants = [dict(row) for row in db.execute(text("SELECT id,org_id,cost_center_id,role,state FROM education_cost_center_grants WHERE user_id=:id ORDER BY id"), params).mappings()]
        db.execute(text("UPDATE education_cost_center_grants SET state='revoked',revoked_by=:actor,revoked_at=CURRENT_TIMESTAMP WHERE user_id=:id AND state='active'"), params)
        db.execute(text('INSERT INTO education_memberships(user_id,org_id,ended_at) VALUES(:id,:org,CURRENT_TIMESTAMP) ON CONFLICT(user_id,org_id) DO UPDATE SET ended_at=CURRENT_TIMESTAMP'), params)
        db.execute(text('UPDATE education_memberships SET ended_at=CURRENT_TIMESTAMP WHERE user_id=:id AND ended_at IS NULL'), params)
        db.execute(text("UPDATE users SET group_id=NULL WHERE id=:id AND role='admin'"), params)
        revoke_sessions_and_biometric(db, user_id, blacklist_hours=ACCESS_TOKEN_EXPIRE_HOURS)
        db.execute(text('DELETE FROM api_tokens WHERE user_id=:id'), params)
        after = dict(db.execute(text('SELECT id,username,role,group_id,is_active FROM users WHERE id=:id'), params).mappings().one())
        if after['role'] != 'admin' or after['group_id'] is not None:
            refuse('Restore state could not be verified')
        log_audit(db, 'user.platform_superadmin_restored', 'user', user_id, {'actor_user_id': actor['id'], 'confirmation': confirmation, 'before': target, 'after': after, 'grants_before': grants, 'grants_after': [{**grant, 'state': 'revoked'} for grant in grants], 'membership_ended': True, 'credentials_revoked': True})
        db.commit()
        return {'status': 'restored', 'user_id': user_id}
    except Exception:
        db.rollback()
        raise
