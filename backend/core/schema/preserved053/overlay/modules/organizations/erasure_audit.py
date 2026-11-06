"""Sign-in history scrubbing within the caller's account erasure transaction."""
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session
from core.audit_erasure import record_erasure


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
