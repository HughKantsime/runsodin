"""SSO sign-in audit outcomes without misclassifying the authentication method."""
from fastapi import HTTPException
from sqlalchemy import text
from core.audit import log_audit
from core.audit_actor import clear_actor, _erased


def prepare_success(db, user_id):
    # Serialize with actor erasure before retaining an SSO username in details.
    if _erased(db.connection(), user_id):
        raise HTTPException(401, 'Invalid authentication session')
    clear_actor(db)


def failure(db, reason, request):
    db.rollback()
    clear_actor(db)
    try:
        username = getattr(request.state, 'oidc_audit_username', None)
        # SQLite must acquire the writer fence before reading the identity. On
        # PostgreSQL _erased locks the user until this audit transaction commits.
        connection = db.connection()
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        user_id = db.execute(text('SELECT id FROM users WHERE username=:username'),
                             {'username': username}).scalar_one_or_none() if username else None
        if user_id is None or _erased(connection, user_id):
            username = None
        else:
            username = db.execute(text('SELECT username FROM users WHERE id=:uid'),
                                  {'uid': user_id}).scalar_one_or_none()
        log_audit(db, 'auth.login_failed', 'user', details={
            'method': 'oidc', 'reason': reason,
            'username': username,
        })
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(503, 'Login audit unavailable. Try again later.') from None
