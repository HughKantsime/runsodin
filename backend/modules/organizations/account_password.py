"""Change the signed-in user's local password in one transaction."""
from fastapi import Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth import hash_password, verify_password
from core.auth_helpers import _check_rate_limit, _is_locked_out, _record_login_attempt, _validate_password
from core.db import get_db
from core.dependencies import log_audit
from core.mfa_login import _locked_user, cancel_pending_enrollments
from core.rbac import require_interactive_session
from modules.organizations.password_reset import invalidate_reset_links, revoke_sessions_and_biometric


def password_account(request: Request, principal: dict = Depends(require_interactive_session()),
                     db: Session = Depends(get_db)):
    jti = getattr(request.state, "authenticated_session_jti", None)
    if not jti:
        raise HTTPException(401, "Sign in again to change your password.")
    user = _locked_user(db, principal["id"])
    if not user or not user.is_active or user.username != principal["username"]:
        raise HTTPException(401, "Sign in again to change your password.")
    if not (user.password_hash or "").strip():
        raise HTTPException(409, "Your password is managed by your single sign-on provider.")
    # A reset or another password change may have revoked this session while we waited for the lock.
    if db.execute(text("SELECT 1 FROM token_blacklist WHERE jti=:jti"), {"jti": jti}).first():
        raise HTTPException(401, "Sign in again to change your password.")
    return user


def change_password(db, user, request, current_password, new_password):
    ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(db, ip):
        raise HTTPException(429, "Too many attempts. Try again in 5 minutes.")
    if _is_locked_out(db, user.username):
        raise HTTPException(423, "Too many attempts. Try again in 15 minutes.")
    try:
        matches = verify_password(current_password, user.password_hash)
    except ValueError:
        matches = False
    if not matches:
        if not _record_login_attempt(db, ip, user.username, False):
            raise HTTPException(503, "Password changes are temporarily unavailable.")
        raise HTTPException(401, "Check your current password and try again.")
    valid, message = _validate_password(new_password)
    if not valid:
        raise HTTPException(400, message)
    try:
        if verify_password(new_password, user.password_hash):
            raise HTTPException(400, "Choose a different new password.")
        replacement = hash_password(new_password)
    except ValueError:
        raise HTTPException(400, "Choose a different new password.") from None
    db.execute(text("UPDATE users SET password_hash=:hash WHERE id=:id"), {"hash": replacement, "id": user.id})
    cancel_pending_enrollments(db, user.id)
    invalidate_reset_links(db, user.id)
    revoke_sessions_and_biometric(db, user.id, keep_jti=request.state.authenticated_session_jti)
    log_audit(db, "user.password_changed", "user", user.id, {"actor_user_id": user.id, "target_user_id": user.id})
    db.commit()
