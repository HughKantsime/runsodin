"""Actual chosen-carrier authority; no valuation owner/read endpoint claim."""
import pytest
from fastapi import Depends, Request
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def read_route(creators):
    from core.db import get_db
    from core.rbac import require_role
    from core import financial_read_authority
    from sqlalchemy.orm import Session
    client, auth, token, _ = creators
    @client.app.get('/api/owned-financial-authority')
    def route(request: Request, actor: dict = Depends(require_role('viewer',scope='read')), db: Session = Depends(get_db)):
        reader=financial_read_authority.verify_financial_reader(db,request,actor)
        return {'id':reader.id,'role':reader.role,'group_id':reader.group_id,'kind':reader.auth_kind}
    return client,auth,token


@pytest.mark.parametrize('scopes,status',[(['read'],200),(['read','read:spools'],200),(['admin'],200),(['read:spools'],403),(['read:jobs'],403),(['write'],403)])
def test_actual_hashed_key_requires_both_current_read_permissions(read_route,scopes,status):
    client,_,token=read_route
    result=client.get('/api/owned-financial-authority',headers=token(scopes))
    assert result.status_code==status,result.text
    if status==200:assert result.json()['kind']=='user_api_token'


def test_actual_jwt_uses_current_org_and_platform_identity(read_route):
    client,auth,_=read_route
    for name,group in [('admin',1),('platform',None)]:
        result=client.get('/api/owned-financial-authority',headers=auth(name))
        assert result.status_code==200,result.text
        assert result.json()['group_id']==group


def test_unbound_principal_is_not_private_read_authority(review_db):
    from core.financial_read_authority import verify_financial_reader
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        verify_financial_reader(review_db,None,{'id':3,'username':'admin','role':'admin','group_id':1,'_auth_kind':'bearer_jwt'})
    assert error.value.status_code==403


@pytest.mark.parametrize('change', ['group','role','inactive'])
def test_current_actor_drift_after_authentication_refuses(read_route, review_db, monkeypatch, change):
    from core import financial_read_authority
    from sqlalchemy import text
    client,auth,_=read_route
    original=financial_read_authority.verify_financial_reader
    def verify(db,request,actor):
        query={'group':'UPDATE users SET group_id=2 WHERE id=:id','role':"UPDATE users SET role='operator' WHERE id=:id",'inactive':'UPDATE users SET is_active=0 WHERE id=:id'}
        db.execute(text(query[change]),{'id':actor['id']})
        return original(db,request,actor)
    monkeypatch.setattr(financial_read_authority,'verify_financial_reader',verify)
    result=client.get('/api/owned-financial-authority',headers=auth())
    assert result.status_code==403,result.text
    review_db.expire_all()
    assert tuple(review_db.execute(text('SELECT role,group_id,is_active FROM users WHERE id=3')).one()) == ('admin',1,1)


@pytest.mark.parametrize('change',['delete','expire','scope'])
def test_selected_key_change_after_auth_refuses(read_route, review_db, monkeypatch, change):
    from core import financial_read_authority
    from sqlalchemy import text
    client,_,token=read_route;headers=token(['read'])
    original=financial_read_authority.verify_financial_reader
    def verify(db,request,actor):
        query={'delete':'DELETE FROM api_tokens WHERE id=:id','expire':"UPDATE api_tokens SET expires_at='2020-01-01' WHERE id=:id",'scope':"UPDATE api_tokens SET scopes='[\"write\"]' WHERE id=:id"}
        db.execute(text(query[change]),{'id':actor['_token_id']})
        return original(db,request,actor)
    monkeypatch.setattr(financial_read_authority,'verify_financial_reader',verify)
    result=client.get('/api/owned-financial-authority',headers=headers)
    assert result.status_code==403,result.text
    review_db.expire_all()
    assert review_db.execute(text("SELECT scopes,expires_at FROM api_tokens WHERE id=(SELECT max(id) FROM api_tokens)")).one()==('["read"]',None)


def test_selected_jwt_blacklist_change_refuses(read_route, review_db, monkeypatch):
    import jwt
    from datetime import datetime,timezone
    from core import auth as auth_module,financial_read_authority
    from sqlalchemy import text
    client,auth,_=read_route;headers=auth()
    payload=jwt.decode(headers['Authorization'].partition(' ')[2],auth_module.SECRET_KEY,algorithms=[auth_module.ALGORITHM])
    original=financial_read_authority.verify_financial_reader
    def verify(db,request,actor):
        db.execute(text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:expiry)'),{'jti':payload['jti'],'expiry':datetime.fromtimestamp(payload['exp'],timezone.utc)})
        return original(db,request,actor)
    monkeypatch.setattr(financial_read_authority,'verify_financial_reader',verify)
    assert client.get('/api/owned-financial-authority',headers=headers).status_code==403
    review_db.expire_all();assert review_db.execute(text('SELECT count(*) FROM token_blacklist WHERE jti=:jti'),{'jti':payload['jti']}).scalar_one()==0


def test_genuine_tracked_cookie_has_priority(read_route,review_db):
    import secrets
    from core.auth import hash_password
    from modules.organizations.routes_auth import router
    from sqlalchemy import text
    client,auth,_=read_route;client.app.include_router(router,prefix='/api')
    password=secrets.token_urlsafe(32)
    review_db.execute(text('UPDATE users SET password_hash=:hash WHERE id=71'),{'hash':hash_password(password)});review_db.commit()
    assert client.post('/api/auth/login',data={'username':'platform','password':password}).status_code==200
    result=client.get('/api/owned-financial-authority',headers=auth('admin'))
    assert result.status_code==200,result.text
    assert result.json()['id']==71 and result.json()['kind']=='session_jwt'


def test_untracked_cookie_never_falls_back_to_valid_bearer(read_route):
    from core.auth import create_access_token
    client,auth,_=read_route
    client.cookies.set('session',create_access_token({'sub':'admin'}))
    result=client.get('/api/owned-financial-authority',headers=auth('platform'))
    assert result.status_code==403,result.text
