"""Audit history scrubbing within the caller's account erasure transaction."""
import json
from sqlalchemy import and_, column, or_, select, table, text, update
from sqlalchemy.orm import Session
from core.audit_erasure import record_erasure
from core.identity_scrub import scrub_json_text


_TOKEN_VERBS = {'api_token_created': 'created', 'api_token_revoked': 'revoked'}
_TOKEN_NAME_ACTIONS = ('api_token.square_setup_granted', 'api_token.square_setup_revoked',
                       'square.product_mapped', 'square.stock_baseline')


def erase_token_names(db: Session, user_id: int):
    """Target token ownership, since different users may choose the same token name."""
    from core.audit_erasure import rewrite
    from core.models import AuditLog
    tokens = table('api_tokens', column('id'), column('user_id'))
    # Token IDs are never automatically reused. Creation history also covers deleted tokens.
    owned_ids = select(tokens.c.id).where(tokens.c.user_id == user_id).union(
        select(AuditLog.entity_id).where(AuditLog.entity_type == 'api_token',
                                         AuditLog.action == 'api_token_created',
                                         column('actor_user_id') == user_id))
    lifecycle = and_(AuditLog.entity_type == 'api_token', AuditLog.action.in_(_TOKEN_VERBS),
                     AuditLog.entity_id.in_(owned_ids))
    attributed = and_(AuditLog.action.in_(_TOKEN_NAME_ACTIONS),
                      AuditLog.details['token_owner_user_id'].as_integer() == user_id)
    rows = db.execute(select(AuditLog.id, AuditLog.action, AuditLog.details).where(or_(lifecycle, attributed)))
    for row in rows:
        if row.action in _TOKEN_VERBS and isinstance(row.details, str):
            details = f"Token '[deleted]' {_TOKEN_VERBS[row.action]}"
        elif isinstance(row.details, dict) and 'token_name' in row.details:
            details = {**row.details, 'token_name': '[deleted]'}
        else:
            continue
        if details != row.details:
            rewrite(db, 'UPDATE audit_logs SET details=:d WHERE id=:id', {'d': json.dumps(details), 'id': row.id})


def scrub_remaining_audit_details(row, identifiers):
    # Token names have already been handled by owner. Other fields, such as an
    # approval note on someone else's token, can still mention the erased user.
    if row.entity_type == 'api_token' and row.action in _TOKEN_VERBS:
        return None
    keep = ('token_name',) if row.action in _TOKEN_NAME_ACTIONS else ()
    return scrub_json_text(row.details, identifiers, keep_fields=keep)


def erase_sso_login_history(db: Session, user_id: int):
    """Scrub SSO details and IP while retaining the historical user entity ID."""
    from core.models import AuditLog
    rows = db.execute(select(AuditLog.id, AuditLog.details).where(
        AuditLog.action == 'auth.login', AuditLog.entity_type == 'user',
        AuditLog.entity_id == user_id,
    )).all()
    with record_erasure(db, "action='auth.login' AND entity_type='user' AND entity_id=:uid", {'uid': user_id}):
        for row in rows:
            details = row.details
            if isinstance(details, dict) and 'username' in details:
                details = {**details, 'username': '[deleted]'}
            db.execute(update(AuditLog).where(AuditLog.id == row.id).values(
                details=details, ip_address=None))


def erase_password_login_history(db: Session, username: str):
    """Match the complete sign-in identity, never a SQL pattern or substring."""
    from core.models import AuditLog

    # Password login queries users.username by equality; email is not an alias.
    # Typed JSON reads/writes work on both SQLite JSON and PostgreSQL JSON.
    # Keep this in the account erasure transaction so a failure rolls it back.
    last_id = 0
    while True:
        rows = db.execute(select(AuditLog.id, AuditLog.action, AuditLog.details).where(
            AuditLog.id > last_id, AuditLog.entity_type == 'user',
            AuditLog.action.in_(('login_success', 'login_failed', 'auth.login_failed')),
        ).order_by(AuditLog.id).limit(500)).all()
        if not rows:
            break
        with record_erasure(db, "id>:low AND id<=:high AND entity_type='user' AND "
                            "action IN ('login_success','login_failed','auth.login_failed')",
                            {'low': last_id, 'high': rows[-1].id}):
            for row in rows:
                details = row.details
                if row.action == 'auth.login_failed':
                    if not isinstance(details, dict) or details.get('username') != username:
                        continue
                    scrubbed = {**details, 'username': '[deleted]'}
                else:
                    prefix = 'Login' if row.action == 'login_success' else 'Failed login'
                    if not isinstance(details, str):
                        continue
                    identity, separator, _ = details.rpartition(' from ')
                    if not separator or identity != f'{prefix}: {username}':
                        continue
                    scrubbed = f'{prefix}: [deleted]'
                db.execute(update(AuditLog).where(AuditLog.id == row.id).values(
                    details=scrubbed, ip_address=None))
        last_id = rows[-1].id
    db.execute(text('DELETE FROM login_attempts WHERE username = :username'), {'username': username})
