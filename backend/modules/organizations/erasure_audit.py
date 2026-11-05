"""Sign-in history scrubbing within the caller's account erasure transaction."""
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session


def erase_sso_login_history(db: Session, user_id: int):
    """Clear IPs on user-linked SSO rows without actor attribution."""
    # SSO sign-in rows name the user by entity, not actor, so erase_actor_history
    # doesn't reach them. Their IP address is personal data: clear it too.
    db.execute(text("UPDATE audit_logs SET ip_address=NULL WHERE action='auth.login' AND entity_type='user' "
                    "AND entity_id=:uid AND actor_user_id IS NULL"), {"uid": user_id})


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
