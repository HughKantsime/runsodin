"""Pending enrollment authority must not outlive credential/session revocation."""
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_password_reset_behavior import reset_client,seed_link,new_password
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('action',['reset','change','revoke_all'])
def test_pending_authority_is_invalidated_atomically(reset_client,review_db,monkeypatch,action):
    from core.mfa_login import login_mfa_response
    from modules.organizations import routes_mfa_enrollment
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,sessions,_=reset_client
    client.app.include_router(routes_mfa_enrollment.router)
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"));review_db.commit()
    grant=login_mfa_response(review_db,1)
    headers={'Authorization':'Bearer '+grant['access_token']}
    assert client.post('/auth/mfa/enrollment/setup',headers=headers).status_code==200
    if action=='reset':
        response=client.post('/auth/reset-password',json={'token':seed_link(review_db),'new_password':new_password()})
    elif action=='change':
        response=client.patch('/users/1',headers=interactive_headers(),json={'password':new_password()})
    else:
        response=client.delete('/sessions',headers=sessions[0])
    assert response.status_code==200
    probe=client.post('/auth/mfa/enrollment/setup',headers=headers)
    assert probe.status_code==401
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text("SELECT COUNT(*) FROM mfa_enrollments WHERE user_id=1 AND state='pending'")).scalar_one()==0
        assert fresh.execute(text("SELECT COUNT(*) FROM mfa_enrollments WHERE user_id=1 AND encrypted_pending_secret IS NOT NULL")).scalar_one()==0


def test_reset_after_password_verification_cannot_mint_new_enrollment(reset_client,review_db,monkeypatch):
    from modules.organizations import routes_auth
    client,_,old_password=reset_client
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"));review_db.commit()
    link=seed_link(review_db);replacement=new_password()
    original=routes_auth._record_login_attempt
    def reset_after_verified(db,ip,username,success):
        recorded=original(db,ip,username,success)
        if success:
            result=client.post('/auth/reset-password',json={'token':link,'new_password':replacement})
            assert result.status_code==200
        return recorded
    monkeypatch.setattr(routes_auth,'_record_login_attempt',reset_after_verified)
    response=client.post('/auth/login',data={'username':'student','password':old_password})
    assert response.status_code==401
    assert 'access_token' not in response.json()
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM mfa_enrollments')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0


@pytest.mark.parametrize('action',['reset','change','revoke_all'])
def test_cancellation_failure_rolls_back_credential_and_session_transaction(reset_client,review_db,monkeypatch,action):
    from core.mfa_login import login_mfa_response
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    from fastapi.testclient import TestClient
    client,sessions,_=reset_client
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"));review_db.commit()
    grant=login_mfa_response(review_db,1)
    from modules.organizations import routes_mfa_enrollment
    client.app.include_router(routes_mfa_enrollment.router)
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    assert client.post('/auth/mfa/enrollment/setup',headers={'Authorization':'Bearer '+grant['access_token']}).status_code==200
    link=seed_link(review_db) if action=='reset' else None
    def snapshot():
        with Session(review_db.get_bind()) as fresh:
            return (fresh.execute(text('SELECT password_hash FROM users WHERE id=1')).scalar_one(),
                    fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one(),
                    fresh.execute(text("SELECT COUNT(*) FROM mfa_enrollments WHERE state='pending'")).scalar_one(),
                    fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one(),
                    fresh.execute(text('SELECT COUNT(*) FROM mfa_enrollments WHERE encrypted_pending_secret IS NOT NULL')).scalar_one())
    before=snapshot()
    original=Session.execute
    def fail_cancel(self,statement,*args,**kwargs):
        if str(statement).startswith("UPDATE mfa_enrollments SET state='cancelled'"):
            raise RuntimeError('synthetic enrollment cancellation failure')
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',fail_cancel)
    with TestClient(client.app,raise_server_exceptions=False) as failure_client:
        if action=='reset':response=failure_client.post('/auth/reset-password',json={'token':link,'new_password':new_password()})
        elif action=='change':response=failure_client.patch('/users/1',headers=interactive_headers(),json={'password':new_password()})
        else:response=failure_client.delete('/sessions',headers=sessions[0])
    assert response.status_code>=400
    unchanged=snapshot()==before
    assert unchanged

@pytest.mark.parametrize('action',['reset','change','revoke_all','admin_mfa','expired'])
def test_revocation_between_enrollment_commit_and_session_issuance_refuses_credentials(reset_client,review_db,monkeypatch,action):
    from core.mfa_login import login_mfa_response
    from modules.organizations import routes_mfa_enrollment
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    import pyotp
    client,sessions,_=reset_client
    client.app.include_router(routes_mfa_enrollment.router)
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"));review_db.commit()
    grant=login_mfa_response(review_db,1)
    headers={'Authorization':'Bearer '+grant['access_token']}
    setup=client.post('/auth/mfa/enrollment/setup',headers=headers);assert setup.status_code==200
    link=seed_link(review_db) if action=='reset' else None
    original=routes_mfa_enrollment.record_session
    def revoke_before_issuance(db,*args,**kwargs):
        if action=='reset':revocation=client.post('/auth/reset-password',json={'token':link,'new_password':new_password()})
        elif action=='change':revocation=client.patch('/users/1',headers=interactive_headers(),json={'password':new_password()})
        elif action=='revoke_all':revocation=client.delete('/sessions',headers=sessions[0])
        elif action=='admin_mfa':revocation=client.delete('/admin/users/1/mfa',headers=interactive_headers())
        else:
            from types import SimpleNamespace
            from core import session_tracking
            monkeypatch.setattr(session_tracking,'time',SimpleNamespace(time=lambda:253402300800.0))
            return original(db,*args,**kwargs)
        assert revocation.status_code==200
        return original(db,*args,**kwargs)
    monkeypatch.setattr(routes_mfa_enrollment,'record_session',revoke_before_issuance)
    response=client.post('/auth/mfa/enrollment/confirm',headers=headers,json={'code':pyotp.TOTP(setup.json()['secret']).now()})
    assert response.status_code==503
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    assert response.json().get('restart_sign_in') is True


def test_invalidation_preserves_original_consumption_timestamp(review_db):
    from core.mfa_login import cancel_pending_enrollments
    review_db.execute(text("INSERT INTO mfa_enrollments(jti,user_id,expires_at,state,created_at,consumed_at) VALUES('synthetic-consumed',1,253402300799,'consumed',100,123)"))
    review_db.commit()
    assert cancel_pending_enrollments(review_db,1)==1
    review_db.commit()
    row=review_db.execute(text("SELECT state,consumed_at FROM mfa_enrollments WHERE jti='synthetic-consumed'")).one()
    assert tuple(row)==('cancelled',123)
