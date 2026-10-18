"""Reset-link issuance, configuration and mail formatting shared by account routes."""
import hashlib
from html import escape
import secrets
from datetime import datetime,timedelta,timezone
from urllib.parse import urlencode,urlsplit

from fastapi import HTTPException
from sqlalchemy import text

from core.db_compat import execute_insert_returning_id
from core.dependencies import log_audit
from core.models import SystemConfig
from core.application_url import configured_base_url, validate_base_url, base_url_health_warning  # noqa: F401  (re-exported)


def reset_token_digest(raw):
    return 'sha256:'+hashlib.sha256(raw.encode('utf-8')).hexdigest()


def reset_mail_base_url(db):
    config=db.query(SystemConfig).filter(SystemConfig.key=='smtp_config').first()
    smtp=config.value if config and isinstance(config.value,dict) else {}
    if smtp.get('enabled') is not True or not isinstance(smtp.get('host'),str) or not smtp['host'].strip():
        raise HTTPException(status_code=503,detail='Password reset requires SMTP to be configured.')
    try:
        return validate_base_url(configured_base_url(db), enforce_https=False)
    except ValueError:
        raise HTTPException(status_code=503,detail='Password reset requires a valid application base URL.') from None


def invalidate_recovery_credentials(db, user_id):
    """Void outstanding reset links and biometric refresh tokens for a user.

    Called when the recovery email changes, so a link or long-lived token
    issued against the old address can no longer be redeemed.
    """
    db.execute(text('UPDATE password_reset_tokens SET used=1 WHERE user_id=:uid AND used=0'),
               {'uid': user_id})
    db.execute(text('DELETE FROM biometric_tokens WHERE user_id=:uid'), {'uid': user_id})


def revoke_sessions_and_biometric(db, user_id, *, keep_jti=None, blacklist_hours=24):
    """Revoke a user's biometric tokens and active sessions after a password change.

    keep_jti, when given, is the one session left alive (the caller's own, for a
    self-initiated change); every other session jti is blacklisted and deleted.
    """
    from core.db_compat import sql
    db.execute(text('DELETE FROM biometric_tokens WHERE user_id=:uid'), {'uid': user_id})
    expiry = (datetime.now(timezone.utc) + timedelta(hours=blacklist_hours)).isoformat()
    rows = db.execute(text('SELECT token_jti FROM active_sessions WHERE user_id=:uid'),
                      {'uid': user_id}).fetchall()
    for session in rows:
        if keep_jti is not None and session.token_jti == keep_jti:
            continue
        db.execute(text(f"{sql.insert_or_ignore_prefix()} token_blacklist (jti, expires_at) "  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragments from core.db_compat sql.* dialect helpers called with literal arguments (closed per-dialect output); values are bound
                        f"VALUES (:jti, :exp){sql.on_conflict_ignore('jti')}"),
                   {'jti': session.token_jti, 'exp': expiry})
    if keep_jti is None:
        db.execute(text('DELETE FROM active_sessions WHERE user_id=:uid'), {'uid': user_id})
    else:
        db.execute(text('DELETE FROM active_sessions WHERE user_id=:uid AND token_jti != :jti'),
                   {'uid': user_id, 'jti': keep_jti})


def current_session_jti(request):
    """The jti of the session/bearer token on this request, or None."""
    if request is None:
        return None
    import jwt as _jwt
    from core import auth as _auth
    cookie = request.cookies.get('session')
    header = request.headers.get('authorization', '')
    raw = cookie or (header.split(' ', 1)[1] if header.startswith('Bearer ') else None)
    if not raw:
        return None
    try:
        return _jwt.decode(raw, _auth.SECRET_KEY, algorithms=[_auth.ALGORITHM]).get('jti')
    except Exception:
        return None


def send_reset_link(db,user_id,username,email,send,*,base_url,actor_user_id=None):
    """Persist a hashed link before delivery; never change credentials while sending."""
    raw=secrets.token_urlsafe(32)
    expires=datetime.now(timezone.utc)+timedelta(hours=1)
    token_id=execute_insert_returning_id(db,
        'INSERT INTO password_reset_tokens(user_id,token,expires_at,used) VALUES(:uid,:token,:expiry,0)',
        {'uid':user_id,'token':reset_token_digest(raw),'expiry':expires.isoformat()})
    details={'actor_user_id':actor_user_id,'target_user_id':user_id}
    log_audit(db,'user.password_reset_requested','user',user_id,details)
    db.commit()
    link=base_url+'/reset-password?'+urlencode({'token':raw})
    context='An administrator requested a password reset for your account.' if actor_user_id is not None else 'A password reset was requested for your account.'
    body=f'''<html><body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2>O.D.I.N. Password Reset</h2>
    <p>{context} <strong>{escape(username)}</strong></p>
    <p><a href="{escape(link,quote=True)}">Click here to reset your password</a></p>
    <p>This link expires in 1 hour. Your password has not changed. If you did not request this, ignore this email.</p>
    </body></html>'''
    try:
        accepted=send(db,email,'O.D.I.N. Password Reset',body) is True
    except Exception:
        accepted=False
    if not accepted:
        db.execute(text('UPDATE password_reset_tokens SET used=1 WHERE id=:id'),{'id':token_id})
    log_audit(db,'user.password_reset_email_accepted' if accepted else 'user.password_reset_email_failed',
              'user',user_id,{**details,'outcome':'accepted' if accepted else 'failed'})
    db.commit()
    return accepted
