"""SSO sign-in audit outcomes without misclassifying the authentication method."""
from fastapi import HTTPException
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
        log_audit(db, 'auth.login_failed', 'user', details={
            'method': 'oidc', 'reason': reason,
            'username': getattr(request.state, 'oidc_audit_username', None),
        })
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(503, 'Login audit unavailable. Try again later.') from None
