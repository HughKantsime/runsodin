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
from core.application_url import configured_base_url, validate_base_url  # noqa: F401  (re-exported)


def reset_token_digest(raw):
    return 'sha256:'+hashlib.sha256(raw.encode('utf-8')).hexdigest()


def reset_mail_base_url(db):
    config=db.query(SystemConfig).filter(SystemConfig.key=='smtp_config').first()
    smtp=config.value if config and isinstance(config.value,dict) else {}
    if smtp.get('enabled') is not True or not isinstance(smtp.get('host'),str) or not smtp['host'].strip():
        raise HTTPException(status_code=503,detail='Password reset requires SMTP to be configured.')
    try:
        return validate_base_url(configured_base_url(db))
    except ValueError:
        raise HTTPException(status_code=503,detail='Password reset requires a valid application base URL.') from None


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
