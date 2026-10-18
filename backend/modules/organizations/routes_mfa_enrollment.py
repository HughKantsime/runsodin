"""Restricted login enrollment. No normal REST/cookie/API-key fallback."""
import base64,io,math,re,time
from datetime import datetime,timezone
import jwt,pyotp,qrcode
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from core import auth
from core.config import settings
from core.crypto import get_fernet
from core.db import get_db
from core.db_compat import sql
from core.dependencies import log_audit
from core.session_tracking import record_session
from core.rate_limit import limiter

router=APIRouter()


def _rollback(db):
    try:db.rollback()
    except Exception:pass


def _commit(db):
    try:db.commit()
    except Exception:
        _rollback(db)
        raise HTTPException(status_code=503,detail='Enrollment could not be confirmed. Restart sign-in.') from None


def _authority(request,db):
    header=request.headers.get('authorization','')
    if not header.startswith('Bearer '):
        raise HTTPException(status_code=401,detail='Enrollment authority required')
    try:
        payload=jwt.decode(header[7:],auth.SECRET_KEY,algorithms=[auth.ALGORITHM],
                           options={'require':['sub','jti','user_id','exp']})
        expiry=payload['exp']
        if (payload.get('mfa_setup_required') is not True or 'mfa_pending' in payload
            or not isinstance(payload['sub'],str) or not payload['sub']
            or not isinstance(payload['jti'],str) or not payload['jti']
            or type(payload['user_id']) is not int or payload['user_id']<=0
            or type(expiry) not in (int,float) or not math.isfinite(expiry)
            or not 0<expiry<=253402300799 or expiry<=time.time()):
            raise ValueError('Invalid authority')
    except Exception:
        raise HTTPException(status_code=401,detail='Invalid or expired enrollment authority') from None
    # All operations take the user lock before the authority lock/write.
    dialect=db.get_bind().dialect.name
    if dialect=='sqlite':
        connection=db.connection().connection.driver_connection
        if not connection.in_transaction:db.execute(text('BEGIN IMMEDIATE'))
    suffix=' FOR UPDATE' if dialect=='postgresql' else ''
    user=db.execute(text('SELECT * FROM users WHERE id=:id AND username=:sub'+suffix),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: lock suffix selected from the closed set {'', ' FOR UPDATE'}; values are bound
                    {'id':payload['user_id'],'sub':payload['sub']}).fetchone()
    if not user or not user.is_active or user.mfa_enabled:
        raise HTTPException(status_code=401,detail='Enrollment is no longer available. Restart sign-in.')
    row=db.execute(text('SELECT * FROM mfa_enrollments WHERE jti=:jti'+suffix),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: lock suffix selected from the closed set {'', ' FOR UPDATE'}; values are bound
                   {'jti':payload['jti']}).fetchone()
    revoked=db.execute(text('SELECT 1 FROM token_blacklist WHERE jti=:jti'),{'jti':payload['jti']}).fetchone()
    if (not row or row.user_id!=user.id or row.state!='pending'
        or row.expires_at!=expiry or row.expires_at<=time.time() or revoked):
        raise HTTPException(status_code=401,detail='Enrollment is no longer available. Restart sign-in.')
    return payload,user,row


def _fernet():
    key=get_fernet()
    if key is None:raise HTTPException(status_code=503,detail='Enrollment encryption is unavailable. Restart sign-in.')
    return key


def _secret(ciphertext):
    try:
        secret=_fernet().decrypt(ciphertext.encode()).decode()
        if not re.fullmatch('[A-Z2-7]{32}',secret):raise ValueError('Invalid secret')
        return secret
    except Exception:
        raise HTTPException(status_code=503,detail='Enrollment encryption is unavailable. Restart sign-in.') from None


@router.post('/auth/mfa/enrollment/setup',tags=['Auth'])
@limiter.limit('10/minute')
def setup(request:Request,db:Session=Depends(get_db)):
    try:
        payload,user,row=_authority(request,db)
        ciphertext=row.encrypted_pending_secret
        if ciphertext is None:
            ciphertext=_fernet().encrypt(pyotp.random_base32().encode()).decode()
            changed=db.execute(text("UPDATE mfa_enrollments SET encrypted_pending_secret=:secret WHERE jti=:jti AND state='pending' AND encrypted_pending_secret IS NULL AND expires_at>:now"),
                               {'secret':ciphertext,'jti':payload['jti'],'now':time.time()})
            if changed.rowcount!=1:raise HTTPException(status_code=409,detail='Enrollment changed. Restart sign-in.')
        secret=_secret(ciphertext)
        _commit(db)
        uri=pyotp.TOTP(secret).provisioning_uri(name=user.username,issuer_name='O.D.I.N.')
        buffer=io.BytesIO();qrcode.make(uri).save(buffer,format='PNG')
        return {'secret':secret,'qr_code':'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode(),
                'expires_at':payload['exp']}
    except HTTPException:
        _rollback(db);raise
    except Exception:
        _rollback(db)
        raise HTTPException(status_code=503,detail='Enrollment could not be confirmed. Restart sign-in.') from None


@router.post('/auth/mfa/enrollment/confirm',tags=['Auth'])
@limiter.limit('10/minute')
def confirm(body:dict,request:Request,db:Session=Depends(get_db)):
    try:
        payload,user,row=_authority(request,db)
        code=body.get('code','')
        if not isinstance(code,str) or not re.fullmatch('[0-9]{6}',code) or not row.encrypted_pending_secret:
            raise HTTPException(status_code=400,detail='Set up an authenticator and enter its six-digit code')
        secret=_secret(row.encrypted_pending_secret)
        if not pyotp.TOTP(secret).verify(code,valid_window=1):
            raise HTTPException(status_code=400,detail='Invalid MFA code')
        now=time.time()
        claimed=db.execute(text("UPDATE mfa_enrollments SET state='consumed',encrypted_pending_secret=NULL,consumed_at=:now WHERE jti=:jti AND user_id=:uid AND state='pending' AND expires_at=:exp AND expires_at>:now AND encrypted_pending_secret=:secret"),
                           {'now':now,'jti':payload['jti'],'uid':user.id,'exp':payload['exp'],'secret':row.encrypted_pending_secret})
        changed=db.execute(text('UPDATE users SET mfa_enabled=TRUE,mfa_secret=:secret WHERE id=:id AND username=:sub AND is_active=TRUE AND mfa_enabled=FALSE'),
                           {'secret':row.encrypted_pending_secret,'id':user.id,'sub':payload['sub']})
        used=db.execute(text(f"{sql.insert_or_ignore_prefix()} token_blacklist(jti,expires_at) VALUES(:jti,:exp){sql.on_conflict_ignore('jti')}"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragments from core.db_compat sql.* dialect helpers called with literal arguments (closed per-dialect output); values are bound
                        {'jti':payload['jti'],'exp':datetime.fromtimestamp(payload['exp'],tz=timezone.utc).isoformat()})
        if claimed.rowcount!=1 or changed.rowcount!=1 or used.rowcount!=1:
            raise HTTPException(status_code=409,detail='Enrollment changed. Restart sign-in.')
        log_audit(db,'mfa.enrollment_completed','user',user.id,ip=request.client.host if request.client else None)
        db.commit()
    except HTTPException:
        _rollback(db);raise
    except Exception:
        _rollback(db)
        raise HTTPException(status_code=503,detail='Enrollment could not be confirmed. Restart sign-in.') from None
    token=auth.create_access_token({'sub':user.username,'role':user.role})
    if record_session(db,user.id,token,request.client.host if request.client else 'unknown',request.headers.get('user-agent',''),payload['jti']) is not True:
        return JSONResponse(status_code=503,content={'detail':'MFA is enrolled, but sign-in could not be confirmed. Restart sign-in.','restart_sign_in':True})
    response=JSONResponse({'access_token':token,'token_type':'bearer'})
    response.set_cookie('session',token,httponly=True,secure=settings.cookie_secure,samesite=settings.cookie_samesite,path='/',max_age=86400)
    return response


@router.post('/auth/mfa/enrollment/cancel',tags=['Auth'])
@limiter.limit('10/minute')
def cancel(request:Request,db:Session=Depends(get_db)):
    try:
        payload,user,row=_authority(request,db)
        changed=db.execute(text("UPDATE mfa_enrollments SET state='cancelled',encrypted_pending_secret=NULL,consumed_at=:now WHERE jti=:jti AND user_id=:uid AND state='pending' AND expires_at>:now"),
                           {'now':time.time(),'jti':payload['jti'],'uid':user.id})
        if changed.rowcount!=1:raise HTTPException(status_code=409,detail='Enrollment changed. Restart sign-in.')
        _commit(db)
        return {'status':'cancelled'}
    except HTTPException:
        _rollback(db);raise
    except Exception:
        _rollback(db)
        raise HTTPException(status_code=503,detail='Enrollment could not be confirmed. Restart sign-in.') from None
