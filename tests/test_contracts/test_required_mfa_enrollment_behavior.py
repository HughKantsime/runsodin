"""Required MFA must offer a usable restricted enrollment authority."""
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_mfa_recovery_codes import make_app


@pytest.mark.parametrize('with_session_cookie', [False, True])
def test_required_setup_token_can_reach_enrollment_but_not_normal_rest(review_db,monkeypatch,caplog,with_session_cookie):
    from core.auth import create_access_token, hash_password
    import secrets
    from modules.organizations import routes_mfa_enrollment
    app=make_app(review_db.get_bind(), monkeypatch)
    app.include_router(routes_mfa_enrollment.router)
    password=secrets.token_urlsafe(24)+'aA1'
    review_db.get_bind().hide_parameters=True
    review_db.execute(text('UPDATE users SET password_hash=:hash WHERE id=1'),{'hash':hash_password(password)})
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"))
    review_db.commit()
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
    caplog.set_level(logging.DEBUG)
    confirm_headers = {**headers, 'Idempotency-Key': 'enrollment-confirm'}
    if with_session_cookie:
        # A normal cookie gives the idempotency middleware a cache owner.
        confirm_headers['Cookie'] = 'session=' + create_access_token({'sub': 'student', 'role': 'viewer'})
    confirmation=client.post('/auth/mfa/enrollment/confirm',headers=confirm_headers,json={'code':pyotp.TOTP(secret).now()})
    assert confirmation.status_code==200
    assert len(set(confirmation.json()['recovery_codes']))==10
    metadata=client.get('/auth/mfa/recovery-codes').json()
    assert set(metadata)=={'remaining','generated_at'} and metadata['remaining']==10
    assert client.get('/auth/me').status_code==200
    client.cookies.clear()
    assert client.post('/auth/mfa/enrollment/confirm',headers=headers,json={'code':pyotp.TOTP(secret).now()}).status_code==401
    with Session(review_db.get_bind()) as fresh:
        cache = fresh.execute(text('SELECT state,response_body FROM idempotency_keys')).all()
        assert len(cache) == int(with_session_cookie)
        assert all(row.state == 'uncacheable_success' and not row.response_body for row in cache)
        audit = fresh.execute(text('SELECT * FROM audit_logs')).all()
        assert any(row.action == 'mfa.enrollment_completed' for row in audit)
        captured = (caplog.text + str(audit)).upper()
        leaked = any(c in captured or c.replace('-', '') in captured for c in confirmation.json()['recovery_codes'])
        assert not leaked
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==1
        assert fresh.execute(text('SELECT mfa_enabled FROM users WHERE id=1')).scalar_one()==1
        assert fresh.execute(text("SELECT COUNT(*) FROM mfa_enrollments WHERE state='consumed' AND encrypted_pending_secret IS NULL")).scalar_one()==1
