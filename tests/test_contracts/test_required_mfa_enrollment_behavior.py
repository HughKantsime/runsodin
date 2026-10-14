"""Required MFA must offer a usable restricted enrollment authority."""
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db


def test_required_setup_token_can_reach_enrollment_but_not_normal_rest(review_db,monkeypatch):
    from core.db import get_db
    from core.auth import hash_password
    import secrets
    from modules.organizations import routes_auth, routes_mfa_enrollment
    monkeypatch.delenv('API_KEY',raising=False)
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    app=FastAPI();app.include_router(routes_auth.router);app.include_router(routes_mfa_enrollment.router)
    def database():
        with Session(review_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    password=secrets.token_urlsafe(24)+'aA1'
    review_db.get_bind().hide_parameters=True
    review_db.execute(text('UPDATE users SET password_hash=:hash WHERE id=1'),{'hash':hash_password(password)})
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"))
    review_db.commit()
    routes_auth.limiter._storage.reset()
    client=TestClient(app,raise_server_exceptions=False,base_url='https://testserver')
    login=client.post('/auth/login',data={'username':'student','password':password})
    assert login.status_code==200 and login.json().get('mfa_setup_required') is True
    headers={'Authorization':'Bearer '+login.json()['access_token']}
    assert client.get('/auth/me',headers=headers).status_code==401
    response=client.post('/auth/mfa/enrollment/setup',headers=headers)
    assert response.status_code==200
    import pyotp
    secret=response.json()['secret']
    again=client.post('/auth/mfa/enrollment/setup',headers=headers)
    stable_secret=again.status_code==200 and again.json()['secret']==secret
    assert stable_secret
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT mfa_secret FROM users WHERE id=1')).scalar_one() is None
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
    confirmation=client.post('/auth/mfa/enrollment/confirm',headers=headers,json={'code':pyotp.TOTP(secret).now()})
    assert confirmation.status_code==200
    assert client.get('/auth/me').status_code==200
    client.cookies.clear()
    assert client.post('/auth/mfa/enrollment/confirm',headers=headers,json={'code':pyotp.TOTP(secret).now()}).status_code==401
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==1
        assert fresh.execute(text('SELECT mfa_enabled FROM users WHERE id=1')).scalar_one()==1
        assert fresh.execute(text("SELECT COUNT(*) FROM mfa_enrollments WHERE state='consumed' AND encrypted_pending_secret IS NULL")).scalar_one()==1
