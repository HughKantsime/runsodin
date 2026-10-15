"""Owned HTTP admin MFA recovery authorization across organization boundaries."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def admin_mfa_client(review_db,monkeypatch):
    from core.db import get_db
    from modules.organizations import routes_auth, routes_users
    monkeypatch.delenv('API_KEY',raising=False)
    app=FastAPI();app.include_router(routes_auth.router);app.include_router(routes_users.router)
    def database():
        with Session(review_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    review_db.execute(text("UPDATE users SET mfa_enabled=1,mfa_secret='synthetic-unusable-ciphertext' WHERE id IN(1,5)"))
    review_db.commit()
    return TestClient(app)


def principal(username,role):
    from core.auth import create_access_token
    return {'Authorization':'Bearer '+create_access_token({'sub':username,'role':role})}


def test_admin_can_disable_own_org_mfa_and_audits(admin_mfa_client,review_db):
    response=admin_mfa_client.delete('/admin/users/1/mfa',headers=principal('admin','admin'))
    assert response.status_code==200
    with Session(review_db.get_bind()) as fresh:
        assert tuple(fresh.execute(text('SELECT mfa_enabled,mfa_secret FROM users WHERE id=1')).one())==(0,None)
        assert fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='mfa_disabled_admin'")).scalar_one()==1


def test_viewer_cannot_disable_mfa(admin_mfa_client,review_db):
    response=admin_mfa_client.delete('/admin/users/1/mfa',headers=principal('student','viewer'))
    assert response.status_code==403
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT mfa_enabled FROM users WHERE id=1')).scalar_one()==1


def test_org_admin_cannot_disable_foreign_org_mfa(admin_mfa_client,review_db):
    response=admin_mfa_client.delete('/admin/users/5/mfa',headers=principal('admin','admin'))
    assert response.status_code in (403,404)
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT mfa_enabled FROM users WHERE id=5')).scalar_one()==1
        assert fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='mfa_disabled_admin'")).scalar_one()==0


@pytest.mark.parametrize('scope',['read','write','admin'])
def test_no_api_token_recovers_mfa(admin_mfa_client,review_db,scope):
    # Security review HIGH 1: MFA recovery is credential material; no token, admin included, may do it.
    import secrets,json
    from core.auth import hash_password
    raw='odin_'+secrets.token_urlsafe(32)
    review_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'Synthetic recovery',:hash,:prefix,:scopes)"),
                      {'hash':hash_password(raw),'prefix':raw[:10],'scopes':json.dumps([scope])})
    review_db.commit()
    response=admin_mfa_client.delete('/admin/users/1/mfa',headers={'X-API-Key':raw})
    assert response.status_code==403
    assert response.json()['detail']=="API tokens cannot create or change credentials; sign in with a password session"
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT mfa_enabled FROM users WHERE id=1')).scalar_one()==1


def test_global_admin_retains_foreign_recovery(admin_mfa_client,review_db):
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'));review_db.commit()
    assert admin_mfa_client.delete('/admin/users/5/mfa',headers=principal('admin','admin')).status_code==200


def test_org_admin_cannot_recover_unassigned_user(admin_mfa_client,review_db):
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=5'));review_db.commit()
    response=admin_mfa_client.delete('/admin/users/5/mfa',headers=principal('admin','admin'))
    assert response.status_code==403
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT mfa_enabled FROM users WHERE id=5')).scalar_one()==1


def test_audit_failure_rolls_back_mfa_secret_change(admin_mfa_client,review_db,monkeypatch):
    from modules.organizations import routes_auth
    def fail(*args,**kwargs):raise RuntimeError('synthetic recovery audit failure')
    monkeypatch.setattr(routes_auth,'log_audit',fail)
    response=admin_mfa_client.delete('/admin/users/1/mfa',headers=principal('admin','admin'))
    assert response.status_code==503
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT mfa_enabled FROM users WHERE id=1')).scalar_one()==1
        assert fresh.execute(text('SELECT mfa_secret FROM users WHERE id=1')).scalar_one()=='synthetic-unusable-ciphertext'


def test_tenant_move_between_check_and_update_cannot_cross_boundary(admin_mfa_client,review_db,monkeypatch):
    original=Session.execute
    moved=False
    def move_before_write(self,statement,*args,**kwargs):
        nonlocal moved
        if not moved and str(statement).startswith('UPDATE users SET mfa_enabled = FALSE'):
            moved=True
            with review_db.get_bind().begin() as independent:
                independent.execute(text('UPDATE users SET group_id=2 WHERE id=1'))
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',move_before_write)
    response=admin_mfa_client.delete('/admin/users/1/mfa',headers=principal('admin','admin'))
    assert response.status_code==409
    with Session(review_db.get_bind()) as fresh:
        assert tuple(fresh.execute(text('SELECT group_id,mfa_enabled FROM users WHERE id=1')).one())==(2,1)
        assert fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='mfa_disabled_admin'")).scalar_one()==0


def test_scoped_user_list_exposes_mfa_boolean_without_secret(admin_mfa_client):
    response=admin_mfa_client.get('/users',headers=principal('admin','admin'))
    assert response.status_code==200
    rows=response.json()
    assert any(r['id']==1 and r['mfa_enabled'] is True for r in rows)
    assert all(type(r['mfa_enabled']) is bool for r in rows)
    assert all('mfa_secret' not in r and 'password_hash' not in r for r in rows)
    assert all(r['id']!=5 for r in rows)
