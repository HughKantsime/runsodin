"""Actual scoped-token authentication must not mint broader credentials."""
import json
import secrets
import pytest
from fastapi import FastAPI,Depends
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def token_client(review_db,monkeypatch):
    from core.auth import hash_password
    from core.db import get_db
    from core.rbac import require_role
    from modules.organizations import routes_sessions
    monkeypatch.delenv('API_KEY',raising=False)
    routes_sessions.limiter._storage.reset()
    app=FastAPI();app.include_router(routes_sessions.router)
    from core.errors import OdinError
    from fastapi.responses import JSONResponse
    @app.exception_handler(OdinError)
    async def structured_error(request,error):
        return JSONResponse(status_code=error.status,content=error.to_envelope())
    def database():
        with Session(review_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    @app.post('/protected-write')
    def protected(current_user=Depends(require_role('operator',scope='write'))):
        return {'authorized':True}
    def issue(scopes,expiry=None):
        raw='odin_'+secrets.token_urlsafe(32)
        review_db.execute(text('INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes,expires_at) VALUES(3,\'Synthetic parent\',:hash,:prefix,:scopes,:expiry)'),{'hash':hash_password(raw),'prefix':raw[:10],'scopes':json.dumps(scopes),'expiry':expiry})
        review_db.commit()
        return {'X-API-Key':raw}
    return TestClient(app),issue

@pytest.mark.parametrize('requested',[['write'],['admin'],['agent:write'],[]])
def test_read_token_cannot_mint_a_more_powerful_credential(token_client,review_db,requested):
    client,issue=token_client;headers=issue(['read'])
    assert client.post('/protected-write',headers=headers).status_code==403
    before=review_db.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one()
    response=client.post('/tokens',headers=headers,json={'name':'Synthetic child','scopes':requested})
    # Never print successful mint responses: they contain generated credentials.
    assert response.status_code==403
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one()==before


def test_write_token_cannot_delegate_admin_permissions(token_client,review_db):
    client,issue=token_client;headers=issue(['write'])
    assert client.post('/protected-write',headers=headers).status_code==200
    before=review_db.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one()
    response=client.post('/tokens',headers=headers,json={'name':'Synthetic child','scopes':['admin']})
    assert response.status_code==403
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one()==before


def test_empty_scoped_token_does_not_bypass_write_guard(token_client):
    client,issue=token_client
    response=client.post('/protected-write',headers=issue([]))
    assert response.status_code in (401,403)


def interactive_headers():
    from core.auth import create_access_token
    return {'Authorization':'Bearer '+create_access_token(data={'sub':'admin','user_id':3,'role':'admin','group_id':1})}


def test_interactive_mint_use_list_and_revoke_without_credential_disclosure(token_client):
    client,_=token_client;headers=interactive_headers()
    response=client.post('/tokens',headers=headers,json={'name':'Synthetic interactive token','scopes':['write'],'expires_days':7})
    assert response.status_code==200
    data=response.json();child={'X-API-Key':data['token']}
    assert client.post('/protected-write',headers=child).status_code==200
    listing=client.get('/tokens',headers=headers)
    assert listing.status_code==200
    rows=listing.json()
    assert any(row['id']==data['id'] for row in rows)
    assert all('token' not in row and 'token_hash' not in row for row in rows)
    assert client.delete(f"/tokens/{data['id']}",headers=headers).status_code==200
    assert client.post('/protected-write',headers=child).status_code==401


@pytest.mark.parametrize('days',[None,1,30])
def test_admin_delegation_cannot_outlive_parent(token_client,days):
    from datetime import datetime,timedelta,timezone
    client,issue=token_client
    parent_expiry=datetime.now(timezone.utc)+timedelta(hours=1)
    headers=issue(['admin'],parent_expiry.isoformat())
    response=client.post('/tokens',headers=headers,json={'name':'Synthetic bounded child','scopes':['write'],'expires_days':days})
    assert response.status_code==200
    data=response.json()
    assert datetime.fromisoformat(data['expires_at'])<=parent_expiry
    assert client.post('/protected-write',headers={'X-API-Key':data['token']}).status_code==200


@pytest.mark.parametrize('body',[{}, {'scopes':[]},{'scopes':[{}]},{'scopes':[['write']]},{'scopes':['unknown']},{'scopes':'admin'},{'scopes':['write'],'expires_days':'bad'},{'scopes':['write'],'expires_days':-1},{'scopes':['write'],'expires_days':True},{'scopes':['write'],'expires_days':10**20}])
def test_invalid_mint_input_is_rejected_without_rows(token_client,review_db,body):
    client,_=token_client
    before=review_db.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one()
    response=client.post('/tokens',headers=interactive_headers(),json={'name':'Synthetic invalid',**body})
    assert response.status_code==400
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one()==before


@pytest.mark.parametrize('scopes',[[],{},'admin',[1],['unknown'],['read','unknown']])
def test_malformed_stored_scopes_fail_closed(token_client,scopes):
    client,issue=token_client
    response=client.post('/protected-write',headers=issue(scopes))
    assert response.status_code==401


def test_malformed_stored_expiry_fails_closed(token_client):
    client,issue=token_client
    assert client.post('/protected-write',headers=issue(['write'],'not-a-date')).status_code==401


def test_read_token_cannot_revoke_other_credentials(token_client,review_db):
    client,issue=token_client
    read=issue(['read']);issue(['write'])
    target=review_db.execute(text('SELECT MAX(id) FROM api_tokens')).scalar_one()
    before=review_db.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one()
    assert client.delete(f'/tokens/{target}',headers=read).status_code==403
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one()==before


@pytest.mark.parametrize('scopes',[['read'],['write:jobs']])
def test_scoped_token_cannot_revoke_admin_session(token_client,review_db,scopes):
    client,issue=token_client
    review_db.execute(text("INSERT INTO active_sessions(user_id,token_jti) VALUES(3,'synthetic-session-jti')"))
    review_db.commit()
    target=review_db.execute(text('SELECT MAX(id) FROM active_sessions')).scalar_one()
    response=client.delete(f'/admin/sessions/{target}',headers=issue(scopes))
    assert response.status_code==403
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE id=:id'),{'id':target}).scalar_one()==1


def test_grouped_admin_cannot_revoke_foreign_token(token_client,review_db):
    client,issue=token_client
    issue(['write'])
    target=review_db.execute(text('SELECT MAX(id) FROM api_tokens')).scalar_one()
    review_db.execute(text('UPDATE api_tokens SET user_id=5 WHERE id=:id'),{'id':target})
    review_db.commit()
    assert client.delete(f'/tokens/{target}',headers=interactive_headers()).status_code==403
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM api_tokens WHERE id=:id'),{'id':target}).scalar_one()==1


def test_admin_token_can_revoke_own_session(token_client,review_db):
    import jwt
    from core import auth
    client,issue=token_client
    session=auth.create_access_token({'sub':'admin','role':'admin'})
    jti=jwt.decode(session,auth.SECRET_KEY,algorithms=[auth.ALGORITHM])['jti']
    review_db.execute(text("INSERT INTO active_sessions(user_id,token_jti) VALUES(3,:jti)"),{'jti':jti})
    review_db.commit()
    target=review_db.execute(text('SELECT MAX(id) FROM active_sessions')).scalar_one()
    bearer={'Authorization':'Bearer '+session}
    response=client.post('/protected-write',headers=bearer)
    assert response.status_code==200
    response=client.delete(f'/admin/sessions/{target}',headers=issue(['admin']))
    assert response.status_code==200
    response=client.post('/protected-write',headers=bearer)
    assert response.status_code==401
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE id=:id'),{'id':target}).scalar_one()==0


@pytest.mark.parametrize('scopes',[['read'],['write'],['write:jobs'],['agent:write']])
@pytest.mark.parametrize('route',['/sessions/{id}','/sessions','/admin/sessions/{id}','/tokens/{token}'])
def test_non_admin_scopes_cannot_change_credentials(token_client,review_db,scopes,route):
    client,issue=token_client
    headers=issue(scopes)
    token=review_db.execute(text('SELECT MAX(id) FROM api_tokens')).scalar_one()
    review_db.execute(text("INSERT INTO active_sessions(user_id,token_jti) VALUES(3,'synthetic-matrix-session')"))
    review_db.commit()
    sid=review_db.execute(text('SELECT MAX(id) FROM active_sessions')).scalar_one()
    before={table:review_db.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() for table in ('active_sessions','api_tokens','token_blacklist')}
    response=client.delete(route.format(id=sid,token=token),headers=headers)
    assert response.status_code==403
    with Session(review_db.get_bind()) as fresh:
        assert {table:fresh.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() for table in before}==before


@pytest.mark.parametrize('scopes,role',[(['admin'],'operator'),(['admin'],'viewer'),(['agent:write'],'viewer')])
def test_privileged_token_is_invalid_after_account_role_downgrade(token_client,review_db,scopes,role):
    client,issue=token_client
    headers=issue(scopes)
    review_db.execute(text('UPDATE users SET role=:role WHERE id=3'),{'role':role})
    review_db.commit()
    assert client.get('/tokens',headers=headers).status_code==401


def test_interactive_viewer_can_revoke_own_token_but_not_anothers(token_client,review_db):
    from core.auth import create_access_token
    client,issue=token_client
    issue(['read']);own=review_db.execute(text('SELECT MAX(id) FROM api_tokens')).scalar_one()
    review_db.execute(text('UPDATE api_tokens SET user_id=1 WHERE id=:id'),{'id':own});review_db.commit()
    issue(['read']);other=review_db.execute(text('SELECT MAX(id) FROM api_tokens')).scalar_one()
    headers={'Authorization':'Bearer '+create_access_token({'sub':'student','role':'viewer'})}
    response=client.delete(f'/tokens/{other}',headers=headers)
    assert response.status_code==404
    response=client.delete(f'/tokens/{own}',headers=headers)
    assert response.status_code==200
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM api_tokens WHERE id=:id'),{'id':own}).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM api_tokens WHERE id=:id'),{'id':other}).scalar_one()==1


@pytest.mark.parametrize('superadmin',[False,True])
def test_admin_can_revoke_authorized_other_account_token(token_client,review_db,superadmin):
    client,issue=token_client
    issue(['read']);target=review_db.execute(text('SELECT MAX(id) FROM api_tokens')).scalar_one()
    review_db.execute(text('UPDATE api_tokens SET user_id=:uid WHERE id=:id'),{'uid':5 if superadmin else 1,'id':target})
    if superadmin:review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.commit()
    response=client.delete(f'/tokens/{target}',headers=interactive_headers())
    assert response.status_code==200
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM api_tokens WHERE id=:id'),{'id':target}).scalar_one()==0


@pytest.mark.parametrize('role,scope,allowed',[
    ('viewer','agent:read',True),('viewer','agent:write',False),
    ('operator','agent:read',True),('operator','agent:write',True),
    ('admin','agent:write',True),('viewer','admin',False)])
def test_interactive_agent_token_mint_respects_current_role(token_client,review_db,role,scope,allowed):
    client,_=token_client
    review_db.execute(text('UPDATE users SET role=:role WHERE id=3'),{'role':role});review_db.commit()
    before=review_db.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one()
    response=client.post('/tokens',headers=interactive_headers(),json={'name':'Synthetic role-floor token','scopes':[scope]})
    assert response.status_code==(200 if allowed else 403)
    if allowed:
        child={'X-API-Key':response.json()['token']}
        result=client.get('/tokens',headers=child)
        assert result.status_code==200
    elif scope=='agent:write':
        assert response.json()['error']['code']=='permission_denied'
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one()==before+int(allowed)
