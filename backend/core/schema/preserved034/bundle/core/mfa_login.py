"""Local MFA policy shared by password and SSO exchange."""
from datetime import timedelta
import jwt
from fastapi import HTTPException
from sqlalchemy import text
from core import auth


def _locked_user(db,user_id):
    dialect=db.get_bind().dialect.name
    if dialect=='sqlite':
        connection=db.connection().connection.driver_connection
        if not connection.in_transaction:db.execute(text('BEGIN IMMEDIATE'))
    suffix=' FOR UPDATE' if dialect=='postgresql' else ''
    return db.execute(text('SELECT id,username,role,is_active,mfa_enabled,password_hash FROM users WHERE id=:id'+suffix),{'id':user_id}).fetchone()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: lock suffix selected from the closed set {'', ' FOR UPDATE'}; values are bound


def cancel_pending_enrollments(db,user_id):
    # Join the caller's credential/revocation transaction; never commit here.
    _locked_user(db,user_id)
    import time
    return db.execute(text("UPDATE mfa_enrollments SET state='cancelled',encrypted_pending_secret=NULL,consumed_at=CASE WHEN state='pending' THEN :now ELSE consumed_at END WHERE user_id=:uid AND state IN ('pending','consumed')"),
                      {'now':time.time(),'uid':user_id}).rowcount


def read_required_mfa(db):
    try:
        row=db.execute(text("SELECT value FROM system_config WHERE key='require_mfa'")).fetchone()
    except Exception:
        raise HTTPException(status_code=503,detail='MFA policy is unavailable. Try again later.') from None
    if row is None:return False
    value=row[0]
    if value is True or type(value) is str and value=='true':return True
    if value is False or type(value) is str and value=='false':return False
    raise HTTPException(status_code=503,detail='MFA policy is unavailable. Try again later.')


def login_mfa_response(db,user_id,expected_password_hash=None):
    user=_locked_user(db,user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401,detail='Invalid authentication session')
    if expected_password_hash is not None and user.password_hash!=expected_password_hash:
        raise HTTPException(status_code=401,detail='Credentials changed. Restart sign-in.')
    if user.mfa_enabled:
        token=auth.create_access_token({'sub':user.username,'role':user.role,'mfa_pending':True},
                                      expires_delta=timedelta(minutes=5))
        return {'access_token':token,'token_type':'bearer','mfa_required':True}
    if not read_required_mfa(db):
        return None
    token=auth.create_access_token({'sub':user.username,'role':user.role,'user_id':user.id,'mfa_setup_required':True},
                                  expires_delta=timedelta(minutes=10))
    payload=jwt.decode(token,auth.SECRET_KEY,algorithms=[auth.ALGORITHM])
    import time
    try:
        db.execute(text("INSERT INTO mfa_enrollments(jti,user_id,expires_at,state,created_at) VALUES(:jti,:uid,:exp,'pending',:now)"),
                   {'jti':payload['jti'],'uid':user.id,'exp':payload['exp'],'now':time.time()})
        db.commit()
    except Exception:
        try:db.rollback()
        except Exception:pass
        raise HTTPException(status_code=503,detail='Login temporarily unavailable. Try again later.') from None
    return {'access_token':token,'token_type':'bearer','mfa_setup_required':True}
