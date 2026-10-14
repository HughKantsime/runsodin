"""Actual callback/code exchange/local MFA lifecycle; provider transport is owned substitute."""
from urllib.parse import urlparse, parse_qs
import pytest
import pyotp
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_oidc_identity import oidc_db, _seed_oidc_config, _CallbackHandler

@pytest.mark.parametrize('change',['require_enrollment','already_enrolled','disabled'])
def test_callback_honors_current_account_and_policy_before_full_session(oidc_db,monkeypatch,change):
    from core.db import get_db
    from core.crypto import encrypt
    from modules.organizations import oidc_handler,routes_oidc,routes_auth,routes_mfa_enrollment
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.delenv('API_KEY',raising=False)
    _seed_oidc_config(oidc_db)
    monkeypatch.setattr(oidc_handler,'create_handler_from_config',lambda config,redirect_uri:_CallbackHandler())
    app=FastAPI()
    for router in (routes_oidc.router,routes_auth.router,routes_mfa_enrollment.router):app.include_router(router)
    def database():
        with Session(oidc_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    routes_auth.limiter._storage.reset()
    with TestClient(app,base_url='https://testserver',raise_server_exceptions=False) as client:
        callback=client.get('/auth/oidc/callback?code=fixture-code&state=fixture-state',follow_redirects=False)
        assert callback.status_code==302
        codes=parse_qs(urlparse(callback.headers['location']).query).get('oidc_code',[])
        assert len(codes)==1
        assert client.get('/auth/me').status_code==401
        uid=oidc_db.execute(text("SELECT id FROM users WHERE oidc_subject='subject-1'")).scalar_one()
        secret=pyotp.random_base32()
        if change=='require_enrollment':oidc_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"))
        elif change=='already_enrolled':oidc_db.execute(text('UPDATE users SET mfa_enabled=TRUE,mfa_secret=:secret WHERE id=:id'),{'secret':encrypt(secret),'id':uid})
        else:oidc_db.execute(text('UPDATE users SET is_active=FALSE WHERE id=:id'),{'id':uid})
        oidc_db.commit()
        exchange=client.post('/auth/oidc/exchange',json={'code':codes[0]})
        assert client.get('/auth/me').status_code==401
        assert oidc_db.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        oidc_db.rollback()
        if change=='disabled':assert exchange.status_code==401;return
        assert exchange.status_code==200
        flag='mfa_setup_required' if change=='require_enrollment' else 'mfa_required'
        assert exchange.json().get(flag) is True
        token=exchange.json()['access_token']
        if change=='require_enrollment':
            headers={'Authorization':'Bearer '+token}
            setup=client.post('/auth/mfa/enrollment/setup',headers=headers)
            assert setup.status_code==200
            confirm=client.post('/auth/mfa/enrollment/confirm',headers=headers,json={'code':pyotp.TOTP(setup.json()['secret']).now()})
        else:confirm=client.post('/auth/mfa/verify',json={'mfa_token':token,'code':pyotp.TOTP(secret).now()})
        assert confirm.status_code==200
        me=client.get('/auth/me')
        assert me.status_code==200
        expected_user=me.json().get('id')==uid
        assert expected_user
        assert client.post('/auth/oidc/exchange',json={'code':codes[0]}).status_code==400
