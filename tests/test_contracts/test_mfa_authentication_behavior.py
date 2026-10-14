"""Real MFA enrollment/challenge/replay behavior against disposable HTTP routes."""
import pytest
import pyotp
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def mfa_client(review_db,monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.organizations import routes_auth
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    routes_auth.limiter._storage.reset()
    app=FastAPI();app.include_router(routes_auth.router)
    def request_db():
        with Session(review_db.get_bind()) as session:yield session
    app.dependency_overrides[get_db]=request_db
    app.dependency_overrides[get_current_user]=lambda:{'id':1,'username':'student','role':'viewer','group_id':1,'is_active':True}
    # Rate-limit behavior has its own tests; give each disposable app a fresh limiter key.
    return TestClient(app)


def enroll(client):
    response=client.post('/auth/mfa/setup');assert response.status_code==200
    secret=response.json()['secret'];assert response.json()['qr_code'].startswith('data:image/png;base64,')
    code=pyotp.TOTP(secret).now()
    assert client.post('/auth/mfa/confirm',json={'code':code}).status_code==200
    return secret


def challenge(username="student"):
    from core.auth import create_access_token
    return create_access_token({'sub':username,'role':'viewer','mfa_pending':True})


def test_mfa_enrollment_wrong_code_and_ciphertext(mfa_client,review_db):
    response=mfa_client.post('/auth/mfa/setup');assert response.status_code==200
    secret=response.json()['secret']
    stored=review_db.execute(text('SELECT mfa_secret FROM users WHERE id=1')).scalar_one()
    assert stored!=secret
    from core.crypto import decrypt
    assert decrypt(stored)==secret
    assert mfa_client.post('/auth/mfa/confirm',json={'code':'not-a-code'}).status_code==400
    assert mfa_client.post('/auth/mfa/confirm',json={'code':pyotp.TOTP(secret).now()}).status_code==200


def test_mfa_challenge_wrong_code_then_success_then_replay(mfa_client):
    secret=enroll(mfa_client);token=challenge()
    assert mfa_client.post('/auth/mfa/verify',json={'mfa_token':token,'code':'not-a-code'}).status_code==401
    response=mfa_client.post('/auth/mfa/verify',json={'mfa_token':token,'code':pyotp.TOTP(secret).now()})
    assert response.status_code==200,response.text
    assert response.json()['access_token']!=token
    assert mfa_client.post('/auth/mfa/verify',json={'mfa_token':token,'code':pyotp.TOTP(secret).now()}).status_code==401


@pytest.mark.parametrize('account_change',['disable','delete'])
def test_disabled_user_cannot_finish_existing_mfa_challenge(mfa_client,review_db,account_change):
    secret=enroll(mfa_client);token=challenge()
    if account_change=='disable':
        review_db.execute(text('UPDATE users SET is_active=0 WHERE id=1'))
    else:
        review_db.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active,mfa_enabled,mfa_secret) SELECT 8,'removed-user','removed@example.invalid',password_hash,role,1,mfa_enabled,mfa_secret FROM users WHERE id=1"))
        review_db.commit();token=challenge('removed-user')
        review_db.execute(text('DELETE FROM users WHERE id=8'))
    review_db.commit()
    response=mfa_client.post('/auth/mfa/verify',json={'mfa_token':token,'code':pyotp.TOTP(secret).now()})
    assert response.status_code==401
    assert 'access_token' not in response.json()
    assert 'set-cookie' not in response.headers
    assert review_db.execute(text('SELECT count(*) FROM token_blacklist')).scalar_one()==0
    assert review_db.execute(text('SELECT count(*) FROM active_sessions WHERE user_id=1')).scalar_one()==0


def test_mfa_disable_requires_valid_code(mfa_client,review_db):
    secret=enroll(mfa_client)
    assert mfa_client.request('DELETE','/auth/mfa',json={'code':'not-a-code'}).status_code==401
    assert mfa_client.request('DELETE','/auth/mfa',json={'code':pyotp.TOTP(secret).now()}).status_code==200
    assert tuple(review_db.execute(text('SELECT mfa_enabled,mfa_secret FROM users WHERE id=1')).one())==(0,None)
