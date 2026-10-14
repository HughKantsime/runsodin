"""Owned HTTP session-issuance faults; no real provider or credentials."""
import secrets
from datetime import datetime, timedelta, timezone
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_mfa_authentication_behavior import mfa_client, enroll, challenge


def break_session_insert(monkeypatch):
    original=Session.execute
    def fail(self,statement,*args,**kwargs):
        query=str(statement)
        if 'INSERT' in query and 'active_sessions' in query:
            raise RuntimeError('synthetic session storage failure')
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',fail)


def test_mfa_storage_failure_does_not_release_full_session(mfa_client,review_db,monkeypatch):
    import pyotp
    secret=enroll(mfa_client)
    token=challenge()
    break_session_insert(monkeypatch)
    response=mfa_client.post('/auth/mfa/verify',json={'mfa_token':token,'code':pyotp.TOTP(secret).now()})
    assert response.status_code==503
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==1


@pytest.fixture
def oidc_client(review_db):
    from core.db import get_db
    from modules.organizations import routes_oidc
    app=FastAPI()
    app.include_router(routes_oidc.router)
    def database():
        with Session(review_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    review_db.get_bind().hide_parameters=True
    return TestClient(app,raise_server_exceptions=False)


def seed_code(db):
    from core.auth import create_access_token
    code=secrets.token_urlsafe(32)
    token=create_access_token({'sub':'student','role':'viewer'})
    db.execute(text('INSERT INTO oidc_auth_codes(code,access_token,expires_at) VALUES(:code,:token,:expiry)'),
               {'code':code,'token':token,'expiry':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()})
    db.commit()
    return code


def test_oidc_normal_exchange_records_session(oidc_client,review_db):
    code=seed_code(review_db)
    response=oidc_client.post('/auth/oidc/exchange',json={'code':code})
    assert response.status_code==200
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar_one()==1
        assert fresh.execute(text('SELECT COUNT(*) FROM oidc_auth_codes')).scalar_one()==0


def test_oidc_storage_failure_does_not_release_untracked_session(oidc_client,review_db,monkeypatch):
    code=seed_code(review_db)
    break_session_insert(monkeypatch)
    response=oidc_client.post('/auth/oidc/exchange',json={'code':code})
    assert response.status_code==503
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM oidc_auth_codes')).scalar_one()==0


@pytest.mark.parametrize('mode',['missing_exp','missing_jti','wrong_sub','pending','setup','other_owner','inactive'])
def test_shared_record_rejects_invalid_binding(review_db,mode):
    import jwt
    from core import auth
    from core.session_tracking import record_session
    payload={'sub':'student','role':'viewer','jti':secrets.token_hex(16),
             'exp':int((datetime.now(timezone.utc)+timedelta(minutes=5)).timestamp())}
    if mode=='missing_exp':payload.pop('exp')
    elif mode=='missing_jti':payload.pop('jti')
    elif mode=='wrong_sub':payload['sub']='outsider'
    elif mode=='pending':payload['mfa_pending']=True
    elif mode=='setup':payload['mfa_setup_required']=True
    elif mode=='inactive':review_db.execute(text('UPDATE users SET is_active=0 WHERE id=1'))
    elif mode=='other_owner':
        review_db.execute(text('INSERT INTO active_sessions(user_id,token_jti) VALUES(4,:jti)'),{'jti':payload['jti']})
    review_db.commit()
    token=jwt.encode(payload,auth.SECRET_KEY,algorithm=auth.ALGORITHM)
    assert record_session(review_db,1,token,'192.0.2.1','Synthetic') is False
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar_one()==0


@pytest.mark.parametrize('committed',[False,True])
def test_shared_record_commit_uncertainty_never_confirms(review_db,monkeypatch,committed):
    from core.auth import create_access_token
    from core.session_tracking import record_session
    original=review_db.commit
    def fault():
        if committed:original()
        raise RuntimeError('synthetic session commit uncertainty')
    monkeypatch.setattr(review_db,'commit',fault)
    token=create_access_token({'sub':'student','role':'viewer'})
    assert record_session(review_db,1,token,'192.0.2.1','Synthetic') is False
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar_one()==int(committed)


def break_session_commit(monkeypatch,committed):
    original_execute=Session.execute
    original_commit=Session.commit
    def tracked(self,statement,*args,**kwargs):
        query=str(statement)
        result=original_execute(self,statement,*args,**kwargs)
        if 'INSERT' in query and 'active_sessions' in query:
            self.info['synthetic_session_pending']=True
        return result
    def fault(self):
        if self.info.pop('synthetic_session_pending',False):
            if committed:original_commit(self)
            raise RuntimeError('synthetic session commit uncertainty')
        return original_commit(self)
    monkeypatch.setattr(Session,'execute',tracked)
    monkeypatch.setattr(Session,'commit',fault)


@pytest.mark.parametrize('committed',[False,True])
def test_oidc_commit_uncertainty_refuses_token_and_keeps_code_consumed(oidc_client,review_db,monkeypatch,committed):
    code=seed_code(review_db)
    break_session_commit(monkeypatch,committed)
    response=oidc_client.post('/auth/oidc/exchange',json={'code':code})
    assert response.status_code==503
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==int(committed)
        assert fresh.execute(text('SELECT COUNT(*) FROM oidc_auth_codes')).scalar_one()==0


@pytest.mark.parametrize('committed',[False,True])
def test_mfa_commit_uncertainty_refuses_token_and_keeps_challenge_consumed(mfa_client,review_db,monkeypatch,committed):
    import pyotp
    secret=enroll(mfa_client);token=challenge()
    break_session_commit(monkeypatch,committed)
    response=mfa_client.post('/auth/mfa/verify',json={'mfa_token':token,'code':pyotp.TOTP(secret).now()})
    assert response.status_code==503
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==int(committed)
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==1


def test_oidc_disabled_identity_refused_after_code_consumption(oidc_client,review_db):
    code=seed_code(review_db)
    review_db.execute(text('UPDATE users SET is_active=0 WHERE id=1'));review_db.commit()
    response=oidc_client.post('/auth/oidc/exchange',json={'code':code})
    assert response.status_code==401
    assert 'access_token' not in response.json()
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM oidc_auth_codes')).scalar_one()==0


@pytest.mark.parametrize('mode',['no_exp','no_jti','unknown_user','mfa_pending','mfa_setup'])
def test_oidc_exchange_rejects_noninteractive_identity(oidc_client,review_db,mode):
    import jwt
    from core import auth
    payload={'sub':'student','role':'viewer','jti':secrets.token_hex(16),
             'exp':int((datetime.now(timezone.utc)+timedelta(minutes=5)).timestamp())}
    if mode=='no_exp':payload.pop('exp')
    elif mode=='no_jti':payload.pop('jti')
    elif mode=='unknown_user':payload['sub']='synthetic-missing-user'
    elif mode=='mfa_pending':payload['mfa_pending']=True
    elif mode=='mfa_setup':payload['mfa_setup_required']=True
    code=secrets.token_urlsafe(32)
    token=jwt.encode(payload,auth.SECRET_KEY,algorithm=auth.ALGORITHM)
    review_db.execute(text('INSERT INTO oidc_auth_codes(code,access_token,expires_at) VALUES(:code,:token,:expiry)'),
                      {'code':code,'token':token,'expiry':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()})
    review_db.commit()
    response=oidc_client.post('/auth/oidc/exchange',json={'code':code})
    assert response.status_code==401
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM oidc_auth_codes')).scalar_one()==0


def test_oidc_code_is_single_use_under_simultaneous_exchange(oidc_client,review_db,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    code=seed_code(review_db)
    barrier=Barrier(2)
    original=Session.execute
    def synchronized_read(self,statement,*args,**kwargs):
        result=original(self,statement,*args,**kwargs)
        if str(statement).startswith('SELECT access_token, expires_at FROM oidc_auth_codes WHERE code'):
            # Buffer both owned reads before either request reaches deletion.
            rows=result.fetchall()
            class Buffered:
                def fetchone(self):return rows[0] if rows else None
            barrier.wait(timeout=10)
            return Buffered()
        return result
    monkeypatch.setattr(Session,'execute',synchronized_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:oidc_client.post('/auth/oidc/exchange',json={'code':code}),range(2)))
    assert sorted(r.status_code for r in responses)==[200,400]


def test_oidc_changed_payload_is_not_consumed_from_stale_read(oidc_client,review_db,monkeypatch):
    from core.auth import create_access_token
    code=seed_code(review_db)
    original=Session.execute
    changed=False
    replacement=create_access_token({'sub':'outsider','role':'viewer'})
    def replace_after_read(self,statement,*args,**kwargs):
        nonlocal changed
        result=original(self,statement,*args,**kwargs)
        if not changed and str(statement).startswith('SELECT access_token, expires_at FROM oidc_auth_codes WHERE code'):
            rows=result.fetchall();changed=True
            with review_db.get_bind().begin() as independent:
                independent.execute(text('UPDATE oidc_auth_codes SET access_token=:token WHERE code=:code'),
                                    {'token':replacement,'code':code})
            class Buffered:
                def fetchone(self):return rows[0] if rows else None
            return Buffered()
        return result
    monkeypatch.setattr(Session,'execute',replace_after_read)
    response=oidc_client.post('/auth/oidc/exchange',json={'code':code})
    assert response.status_code==400
    assert 'access_token' not in response.json()
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM oidc_auth_codes WHERE code=:code'),{'code':code}).scalar_one()==1


@pytest.mark.parametrize('committed',[False,True])
def test_oidc_consume_commit_uncertainty_refuses_issuance(oidc_client,review_db,monkeypatch,committed):
    code=seed_code(review_db)
    original=Session.commit
    def fault(self):
        if committed:original(self)
        raise RuntimeError('synthetic code consumption confirmation failure')
    monkeypatch.setattr(Session,'commit',fault)
    response=oidc_client.post('/auth/oidc/exchange',json={'code':code})
    assert response.status_code==503
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM oidc_auth_codes')).scalar_one()==int(not committed)


def test_mfa_challenge_is_single_use_under_simultaneous_verification(mfa_client,review_db,monkeypatch):
    import pyotp
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from modules.organizations import routes_auth
    routes_auth.limiter._storage.reset()
    secret=enroll(mfa_client);token=challenge();code=pyotp.TOTP(secret).now()
    barrier=Barrier(2)
    original=Session.execute
    def synchronized(self,statement,*args,**kwargs):
        result=original(self,statement,*args,**kwargs)
        if str(statement).startswith('SELECT 1 FROM token_blacklist WHERE jti'):
            rows=result.fetchall()
            class Buffered:
                def fetchone(self):return rows[0] if rows else None
            barrier.wait(timeout=10)
            return Buffered()
        return result
    monkeypatch.setattr(Session,'execute',synchronized)
    def verify(_):
        with TestClient(mfa_client.app,raise_server_exceptions=False) as independent:
            return independent.post('/auth/mfa/verify',json={'mfa_token':token,'code':code})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(verify,range(2)))
    assert sorted(r.status_code for r in responses)==[200,401]
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==1
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==1


@pytest.mark.parametrize('mode',['no_exp','no_jti','pending_string','setup'])
def test_mfa_rejects_unbound_challenge(mfa_client,review_db,mode):
    import pyotp,jwt
    from core import auth
    secret=enroll(mfa_client)
    payload={'sub':'student','role':'viewer','jti':secrets.token_hex(16),'mfa_pending':True,
             'exp':int((datetime.now(timezone.utc)+timedelta(minutes=5)).timestamp())}
    if mode=='no_exp':payload.pop('exp')
    elif mode=='no_jti':payload.pop('jti')
    elif mode=='pending_string':payload['mfa_pending']='true'
    elif mode=='setup':payload['mfa_setup_required']=True
    token=jwt.encode(payload,auth.SECRET_KEY,algorithm=auth.ALGORITHM)
    response=mfa_client.post('/auth/mfa/verify',json={'mfa_token':token,'code':pyotp.TOTP(secret).now()})
    assert response.status_code==401
    assert 'access_token' not in response.json()
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==0


@pytest.mark.parametrize('committed',[False,True])
def test_mfa_consumption_commit_uncertainty_refuses_full_session(mfa_client,review_db,monkeypatch,committed):
    import pyotp
    secret=enroll(mfa_client);token=challenge()
    original=Session.commit
    def fault(self):
        if committed:original(self)
        raise RuntimeError('synthetic challenge consume commit uncertainty')
    monkeypatch.setattr(Session,'commit',fault)
    response=mfa_client.post('/auth/mfa/verify',json={'mfa_token':token,'code':pyotp.TOTP(secret).now()})
    assert response.status_code==503
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==int(committed)


def test_mfa_expiry_during_totp_verification_refuses_consumption(mfa_client,review_db,monkeypatch):
    import pyotp
    from modules.organizations import routes_auth
    from core.auth import create_access_token
    secret=enroll(mfa_client)
    token=create_access_token({"sub":"student","role":"viewer","mfa_pending":True},expires_delta=timedelta(minutes=5))
    original=pyotp.TOTP.verify
    class AfterExpiry(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime.now(timezone.utc)+timedelta(hours=1)
    def verifies_then_expires(self,*args,**kwargs):
        accepted=original(self,*args,**kwargs)
        monkeypatch.setattr(routes_auth,'datetime',AfterExpiry)
        return accepted
    monkeypatch.setattr(pyotp.TOTP,'verify',verifies_then_expires)
    response=mfa_client.post('/auth/mfa/verify',json={'mfa_token':token,'code':pyotp.TOTP(secret).now()})
    assert response.status_code==401
    assert 'access_token' not in response.json()
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==0


@pytest.mark.parametrize('mode',['infinite_exp','huge_exp','invalid_role','invalid_subject'])
def test_mfa_malformed_signed_claims_are_cleanly_refused(mfa_client,review_db,mode):
    import jwt,pyotp
    from core import auth
    secret=enroll(mfa_client)
    payload={'sub':'student','role':'viewer','jti':secrets.token_hex(16),'mfa_pending':True,
             'exp':int((datetime.now(timezone.utc)+timedelta(minutes=5)).timestamp())}
    if mode=='infinite_exp':payload['exp']=float('inf')
    elif mode=='huge_exp':payload['exp']=10**100
    elif mode=='invalid_role':payload['role']={'synthetic':'invalid'}
    elif mode=='invalid_subject':payload['sub']={'synthetic':'invalid'}
    token=jwt.encode(payload,auth.SECRET_KEY,algorithm=auth.ALGORITHM)
    with TestClient(mfa_client.app,raise_server_exceptions=False) as client:
        response=client.post('/auth/mfa/verify',json={'mfa_token':token,'code':pyotp.TOTP(secret).now()})
    assert response.status_code==401
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==0


def test_oidc_exchange_honors_required_mfa_enrollment(oidc_client,review_db):
    code=seed_code(review_db)
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"));review_db.commit()
    response=oidc_client.post('/auth/oidc/exchange',json={'code':code})
    assert response.status_code==200
    assert response.json().get('mfa_setup_required') is True
    assert 'session' not in response.cookies
    from core.auth import decode_token
    import jwt
    from core import auth
    issued=response.json().get('access_token')
    payload=jwt.decode(issued,auth.SECRET_KEY,algorithms=[auth.ALGORITHM])
    assert payload.get('mfa_setup_required') is True
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0


def test_oidc_exchange_does_not_bypass_enabled_local_mfa(oidc_client,review_db):
    code=seed_code(review_db)
    review_db.execute(text("UPDATE users SET mfa_enabled=1,mfa_secret='synthetic-unusable' WHERE id=1"));review_db.commit()
    response=oidc_client.post('/auth/oidc/exchange',json={'code':code})
    assert response.status_code==200
    assert response.json().get('mfa_required') is True
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
