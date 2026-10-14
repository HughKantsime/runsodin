"""Owned competing enrollment authorities and fail-closed lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest,pyotp
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def enrollment(review_db,monkeypatch):
    from core.db import get_db
    from core.mfa_login import login_mfa_response
    from core.rate_limit import limiter
    from modules.organizations import routes_mfa_enrollment
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    review_db.get_bind().hide_parameters=True
    limiter._storage.reset()
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"));review_db.commit()
    app=FastAPI();app.include_router(routes_mfa_enrollment.router)
    def database():
        with Session(review_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    class OwnedEnrollment:
        def __repr__(self):return '<owned enrollment; credentials redacted>'
        def grant(self):
            response=login_mfa_response(review_db,1)
            return {'Authorization':'Bearer '+response['access_token']}
        def request(self,path,headers,json=None):
            with TestClient(app,raise_server_exceptions=False,base_url='https://testserver') as client:
                return client.post('/auth/mfa/enrollment/'+path,headers=headers,json=json)
    return OwnedEnrollment()


def test_different_authorities_only_one_secret_can_enroll(enrollment,review_db):
    authorities=[enrollment.grant(),enrollment.grant()]
    secrets=[]
    for header in authorities:
        response=enrollment.request('setup',header);assert response.status_code==200
        secrets.append(response.json()['secret'])
    isolated=secrets[0]!=secrets[1];assert isolated
    barrier=Barrier(2)
    def confirm(index):
        barrier.wait(timeout=10)
        return enrollment.request('confirm',authorities[index],{'code':pyotp.TOTP(secrets[index]).now()})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(confirm,range(2)))
    statuses=sorted(r.status_code for r in responses)
    assert statuses==[200,401]
    winner=next(i for i,r in enumerate(responses) if r.status_code==200)
    from core.crypto import get_fernet
    with Session(review_db.get_bind()) as fresh:
        cipher=fresh.execute(text('SELECT mfa_secret FROM users WHERE id=1')).scalar_one()
        matches=get_fernet().decrypt(cipher.encode()).decode()==secrets[winner];assert matches
        assert fresh.execute(text("SELECT COUNT(*) FROM mfa_enrollments WHERE state='consumed' AND encrypted_pending_secret IS NULL")).scalar_one()==1
        assert fresh.execute(text("SELECT COUNT(*) FROM mfa_enrollments WHERE state='pending'")).scalar_one()==1
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==1


def test_cancel_erases_pending_ciphertext_and_refuses_replay(enrollment,review_db):
    header=enrollment.grant()
    assert enrollment.request('setup',header).status_code==200
    response=enrollment.request('cancel',header);assert response.status_code==200
    assert enrollment.request('setup',header).status_code==401
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text("SELECT COUNT(*) FROM mfa_enrollments WHERE state='cancelled' AND encrypted_pending_secret IS NULL")).scalar_one()==1
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT mfa_enabled FROM users WHERE id=1')).scalar_one()==0


def test_corrupt_pending_ciphertext_never_becomes_a_secret(enrollment,review_db):
    header=enrollment.grant()
    review_db.execute(text("UPDATE mfa_enrollments SET encrypted_pending_secret='synthetic-corrupt-ciphertext'"));review_db.commit()
    response=enrollment.request('setup',header)
    assert response.status_code==503
    assert 'secret' not in response.json()
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0


@pytest.mark.parametrize('mode',['expired','disabled','cancelled'])
def test_invalidated_authority_cannot_complete(enrollment,review_db,monkeypatch,mode):
    from modules.organizations import routes_mfa_enrollment
    header=enrollment.grant()
    setup=enrollment.request('setup',header);assert setup.status_code==200
    code=pyotp.TOTP(setup.json()['secret']).now()
    if mode=='expired':
        monkeypatch.setattr(routes_mfa_enrollment.time,'time',lambda:2_534_023_008_000.0)
    elif mode=='disabled':
        review_db.execute(text('UPDATE users SET is_active=0 WHERE id=1'));review_db.commit()
    else:
        assert enrollment.request('cancel',header).status_code==200
    response=enrollment.request('confirm',header,{'code':code})
    assert response.status_code==401
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies


@pytest.mark.parametrize('mode',['audit','commit','ambiguous_commit','session'])
def test_confirmation_uncertainty_never_returns_full_credentials(enrollment,review_db,monkeypatch,mode):
    from modules.organizations import routes_mfa_enrollment
    header=enrollment.grant()
    setup=enrollment.request('setup',header);assert setup.status_code==200
    code=pyotp.TOTP(setup.json()['secret']).now()
    if mode=='audit':
        def fail(*args,**kwargs):raise RuntimeError('synthetic enrollment audit fault')
        monkeypatch.setattr(routes_mfa_enrollment,'log_audit',fail)
    elif mode=='session':
        monkeypatch.setattr(routes_mfa_enrollment,'record_session',lambda *args:False)
    else:
        original=Session.commit
        def uncertain(self):
            if mode=='ambiguous_commit':original(self)
            raise RuntimeError('synthetic enrollment commit uncertainty')
        monkeypatch.setattr(Session,'commit',uncertain)
    response=enrollment.request('confirm',header,{'code':code})
    assert response.status_code==503
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    committed=mode in ('ambiguous_commit','session')
    with Session(review_db.get_bind()) as fresh:
        assert bool(fresh.execute(text('SELECT mfa_enabled FROM users WHERE id=1')).scalar_one()) is committed
        assert fresh.execute(text("SELECT COUNT(*) FROM mfa_enrollments WHERE state='consumed'")).scalar_one()==int(committed)
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==int(committed)
