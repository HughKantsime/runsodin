"""Password reset acceptance through real auth and disposable database transactions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db


class PrivateResetFixture(tuple):
    def __repr__(self):
        return "<isolated reset fixture; credentials redacted>"


@pytest.fixture
def reset_client(review_db, monkeypatch):
    from core.auth import create_access_token, hash_password
    from core.db import get_db
    from modules.organizations import routes_auth, routes_users, routes_sessions
    monkeypatch.delenv('API_KEY', raising=False)
    review_db.get_bind().hide_parameters=True
    routes_auth.limiter._storage.reset()
    app=FastAPI()
    for router in (routes_auth.router,routes_users.router,routes_sessions.router):
        app.include_router(router)
    def database():
        with Session(review_db.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db]=database
    old_password=secrets.token_urlsafe(24)+'aA1'
    review_db.execute(text('UPDATE users SET password_hash=:h WHERE id=1'),{'h':hash_password(old_password)})
    review_db.commit()
    sessions=[]
    for _ in range(2):
        jwt=create_access_token({'sub':'student','role':'viewer'})
        routes_auth._record_session(review_db,1,jwt,'127.0.0.1','Synthetic reset acceptance')
        sessions.append({'Authorization':'Bearer '+jwt})
    client=TestClient(app)
    return PrivateResetFixture((client,sessions,old_password))


def seed_link(db, expiry=None, user=1, used=False):
    token=secrets.token_urlsafe(32)
    expiry=expiry if expiry is not None else (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    db.execute(text('INSERT INTO password_reset_tokens(user_id,token,expires_at,used) VALUES(:uid,:token,:exp,:used)'),{'uid':user,'token':token,'exp':expiry,'used':int(used)})
    db.commit()
    return token


def new_password():
    return secrets.token_urlsafe(24)+'aA1'


def state(db):
    with Session(db.get_bind()) as fresh:
        return (fresh.execute(text('SELECT password_hash FROM users WHERE id=1')).scalar(),
                tuple(fresh.execute(text('SELECT id,used FROM password_reset_tokens ORDER BY id')).all()),
                fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one(),
                fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one(),
                fresh.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one())


def test_valid_reset_rejects_old_sessions_and_invalidates_other_links(reset_client,review_db):
    from core.auth import verify_password
    client,sessions,_=reset_client
    first=seed_link(review_db);second=seed_link(review_db)
    replacement=new_password()
    assert all(client.get('/auth/me',headers=h).status_code==200 for h in sessions)
    response=client.post('/auth/reset-password',json={'token':first,'new_password':replacement})
    assert response.status_code==200
    assert all(client.get('/auth/me',headers=h).status_code==401 for h in sessions)
    with Session(review_db.get_bind()) as fresh:
        matched=verify_password(replacement,fresh.execute(text('SELECT password_hash FROM users WHERE id=1')).scalar_one())
        assert matched
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==2
        assert fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='user.password_reset'")).scalar_one()==1
    before=state(review_db)
    for token in (first,second):
        response=client.post('/auth/reset-password',json={'token':token,'new_password':new_password()})
        assert response.status_code==400
    unchanged=state(review_db)==before
    assert unchanged


@pytest.mark.parametrize('expiry',['not-a-date','2000-01-01T00:00:00+00:00'])
def test_invalid_expiry_does_not_change_account(reset_client,review_db,expiry):
    client,_,_=reset_client;token=seed_link(review_db,expiry)
    before=state(review_db)
    response=client.post('/auth/reset-password',json={'token':token,'new_password':new_password()})
    assert response.status_code==400
    unchanged=state(review_db)==before
    assert unchanged


def test_exact_expiry_is_rejected(reset_client,review_db,monkeypatch):
    from modules.organizations import routes_auth
    now=datetime.now(timezone.utc)
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls,tz=None):
            return now if tz else now.replace(tzinfo=None)
    monkeypatch.setattr(routes_auth,'datetime',FrozenDatetime)
    client,_,_=reset_client;token=seed_link(review_db,now.isoformat());before=state(review_db)
    response=client.post('/auth/reset-password',json={'token':token,'new_password':new_password()})
    assert response.status_code==400
    unchanged=state(review_db)==before
    assert unchanged


@pytest.mark.parametrize('expiry_kind',['naive','offset'])
def test_valid_timestamp_formats_are_accepted(reset_client,review_db,expiry_kind):
    expiry=datetime.now(timezone.utc)+timedelta(hours=1)
    if expiry_kind=='naive':expiry=expiry.replace(tzinfo=None)
    else:expiry=expiry.astimezone(timezone(timedelta(hours=-4)))
    client,_,_=reset_client;token=seed_link(review_db,expiry.isoformat())
    response=client.post('/auth/reset-password',json={'token':token,'new_password':new_password()})
    assert response.status_code==200


@pytest.mark.parametrize("weak",["short","lowercase1","UPPERCASE1","NoDigitsHere"])
def test_weak_password_does_not_consume_link(reset_client,review_db,weak):
    client,_,_=reset_client;token=seed_link(review_db);before=state(review_db)
    response=client.post('/auth/reset-password',json={'token':token,'new_password':weak})
    assert response.status_code==400
    unchanged=state(review_db)==before
    assert unchanged
    response=client.post('/auth/reset-password',json={'token':token,'new_password':new_password()})
    assert response.status_code==200


@pytest.mark.parametrize('change',['disabled','missing'])
def test_inactive_or_missing_account_cannot_consume_link(reset_client,review_db,change):
    client,_,_=reset_client
    uid=1 if change=='disabled' else 98765
    token=seed_link(review_db,user=uid)
    if change=='disabled':
        review_db.execute(text('UPDATE users SET is_active=FALSE WHERE id=1'));review_db.commit()
    before=state(review_db)
    response=client.post('/auth/reset-password',json={'token':token,'new_password':new_password()})
    assert response.status_code==400
    unchanged=state(review_db)==before
    assert unchanged


@pytest.mark.parametrize('same_link',[True,False])
def test_concurrent_resets_have_one_winner(reset_client,review_db,monkeypatch,same_link):
    client,_,_=reset_client
    first=seed_link(review_db);tokens=[first,first if same_link else seed_link(review_db)]
    barrier=Barrier(2);original=Session.execute
    def synchronize(db,statement,*args,**kwargs):
        if str(statement).lstrip().startswith('SELECT id, user_id, expires_at, used FROM password_reset_tokens'):
            if not db.info.get('reset_acceptance_barrier'):
                db.info['reset_acceptance_barrier']=True
                barrier.wait(timeout=10)
        return original(db,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',synchronize)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(lambda token:client.post('/auth/reset-password',json={'token':token,'new_password':new_password()}).status_code,tokens))
    assert sorted(statuses)==[200,400]
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='user.password_reset'")).scalar_one()==1
        assert fresh.execute(text('SELECT COUNT(*) FROM password_reset_tokens WHERE used=0')).scalar_one()==0


def test_reset_transaction_rolls_back_on_audit_failure(reset_client,review_db,monkeypatch):
    from modules.organizations import routes_auth
    client,_,_=reset_client;token=seed_link(review_db);before=state(review_db)
    def fail(*args,**kwargs):raise RuntimeError('Synthetic audit persistence failure')
    monkeypatch.setattr(routes_auth,'log_audit',fail)
    with pytest.raises(RuntimeError,match='Synthetic audit persistence failure'):
        client.post('/auth/reset-password',json={'token':token,'new_password':new_password()})
    unchanged=state(review_db)==before
    assert unchanged


def admin_headers():
    from core.auth import create_access_token
    return {'Authorization':'Bearer '+create_access_token({'sub':'admin','role':'admin'})}


def configure_mail(db):
    from core.models import SystemConfig
    db.add(SystemConfig(key='smtp_config',value={'enabled':True,'host':'smtp.example.invalid'}))
    db.add(SystemConfig(key='base_url',value='https://odin.example.invalid'))
    db.commit()


@pytest.mark.parametrize('outcome',[False,None,'exception'])
def test_admin_email_failure_preserves_password_and_sessions(reset_client,review_db,monkeypatch,outcome):
    from modules.organizations import routes_users
    client,_,_=reset_client;configure_mail(review_db)
    before=state(review_db)[:1]+state(review_db)[2:4]
    def send(*args):
        if outcome=='exception':raise RuntimeError('Synthetic delivery failure')
        return outcome
    monkeypatch.setattr(routes_users,'_send_odin_email',send)
    response=client.post('/users/1/reset-password-email',headers=admin_headers())
    assert response.status_code==503
    unchanged=state(review_db)[:1]+state(review_db)[2:4]==before
    assert unchanged
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM password_reset_tokens WHERE used=0')).scalar_one()==0


def test_admin_email_sends_link_then_reset_consumes_it(reset_client,review_db,monkeypatch):
    from html import unescape
    import re
    from urllib.parse import urlsplit,parse_qs
    from modules.organizations import routes_users
    client,sessions,_=reset_client;configure_mail(review_db)
    captured=[]
    monkeypatch.setattr(routes_users,'_send_odin_email',lambda db,to,subject,body:captured.append(body) or True)
    before=state(review_db)[:1]+state(review_db)[2:4]
    response=client.post('/users/1/reset-password-email',headers=admin_headers())
    assert response.status_code==200
    unchanged=state(review_db)[:1]+state(review_db)[2:4]==before
    assert unchanged
    assert len(captured)==1
    contains_link='/reset-password?token=' in captured[0]
    assert contains_link
    no_plaintext_password='New Password:' not in captured[0] and 'Temporary Password:' not in captured[0]
    assert no_plaintext_password
    href=re.search(r'href="([^"]+)"',captured[0]).group(1)
    token=parse_qs(urlsplit(unescape(href)).query)['token'][0]
    response=client.post('/auth/reset-password',json={'token':token,'new_password':new_password()})
    assert response.status_code==200
    statuses=[client.get('/auth/me',headers=h).status_code for h in sessions]
    assert statuses==[401,401]


@pytest.mark.parametrize('target',[5,98765])
def test_admin_email_foreign_or_missing_target_does_not_send(reset_client,review_db,monkeypatch,target):
    from modules.organizations import routes_users
    client,_,_=reset_client;configure_mail(review_db);calls=[]
    monkeypatch.setattr(routes_users,'_send_odin_email',lambda *args:calls.append(True) or True)
    before=state(review_db)
    response=client.post(f'/users/{target}/reset-password-email',headers=admin_headers())
    assert response.status_code in (403,404)
    assert not calls
    unchanged=state(review_db)==before
    assert unchanged


@pytest.mark.parametrize('scopes',[['read'],['write'],['write:jobs'],['agent:write']])
def test_scoped_token_cannot_request_admin_reset(reset_client,review_db,monkeypatch,scopes):
    import json
    from core.auth import hash_password
    from modules.organizations import routes_users
    client,_,_=reset_client;configure_mail(review_db);calls=[]
    monkeypatch.setattr(routes_users,'_send_odin_email',lambda *args:calls.append(True) or True)
    token='odin_'+secrets.token_urlsafe(32)
    review_db.execute(text('INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,:name,:hash,:prefix,:scopes)'),{'name':'Synthetic reset caller','hash':hash_password(token),'prefix':token[:10],'scopes':json.dumps(scopes)})
    review_db.commit();before=state(review_db)
    response=client.post('/users/1/reset-password-email',headers={'X-API-Key':token})
    assert response.status_code==403
    assert not calls
    unchanged=state(review_db)==before
    assert unchanged


def test_admin_email_rejects_disabled_account(reset_client,review_db,monkeypatch):
    from modules.organizations import routes_users
    client,_,_=reset_client;configure_mail(review_db);calls=[]
    monkeypatch.setattr(routes_users,'_send_odin_email',lambda *args:calls.append(True) or True)
    review_db.execute(text('UPDATE users SET is_active=FALSE WHERE id=1'));review_db.commit();before=state(review_db)
    response=client.post('/users/1/reset-password-email',headers=admin_headers())
    assert response.status_code==400
    assert not calls
    unchanged=state(review_db)==before
    assert unchanged


@pytest.mark.parametrize('base',['','/relative','javascript:alert(1)','https://name:pass@odin.example.invalid','https://odin.example.invalid?next=x','https://odin.example.invalid#fragment','https://odin.example.invalid\n','https://odin.example.invalid:bad'])
def test_invalid_reset_base_url_prevents_issuance(reset_client,review_db,monkeypatch,base):
    from core.models import SystemConfig
    from modules.organizations import routes_users
    client,_,_=reset_client;configure_mail(review_db)
    config=review_db.query(SystemConfig).filter_by(key='base_url').one();config.value=base;review_db.commit()
    calls=[];monkeypatch.setattr(routes_users,'_send_odin_email',lambda *args:calls.append(True) or True)
    before=state(review_db)
    response=client.post('/users/1/reset-password-email',headers=admin_headers())
    assert response.status_code==503
    assert not calls
    unchanged=state(review_db)==before
    assert unchanged


def test_missing_smtp_prevents_reset_issuance(reset_client,review_db,monkeypatch):
    from modules.organizations import routes_users
    client,_,_=reset_client;calls=[]
    monkeypatch.setattr(routes_users,'_send_odin_email',lambda *args:calls.append(True) or True)
    before=state(review_db)
    response=client.post('/users/1/reset-password-email',headers=admin_headers())
    assert response.status_code==503
    assert not calls
    unchanged=state(review_db)==before
    assert unchanged


def test_stored_digest_cannot_be_used_as_reset_credential(reset_client,review_db,monkeypatch):
    from modules.organizations import routes_users
    client,_,_=reset_client;configure_mail(review_db)
    monkeypatch.setattr(routes_users,'_send_odin_email',lambda *args:True)
    response=client.post('/users/1/reset-password-email',headers=admin_headers())
    assert response.status_code==200
    stored=review_db.execute(text('SELECT token FROM password_reset_tokens')).scalar_one()
    hashed=stored.startswith('sha256:')
    assert hashed
    before=state(review_db)
    response=client.post('/auth/reset-password',json={'token':stored,'new_password':new_password()})
    assert response.status_code==400
    unchanged=state(review_db)==before
    assert unchanged


def test_reset_email_escapes_account_name(reset_client,review_db,monkeypatch):
    from modules.organizations import routes_users
    client,_,_=reset_client;configure_mail(review_db)
    review_db.execute(text('UPDATE users SET username=:name WHERE id=1'),{'name':'<img src=x onerror=alert(1)>'});review_db.commit()
    captured=[];monkeypatch.setattr(routes_users,'_send_odin_email',lambda db,to,subject,body:captured.append(body) or True)
    response=client.post('/users/1/reset-password-email',headers=admin_headers())
    assert response.status_code==200
    escaped='&lt;img src=x onerror=alert(1)&gt;' in captured[0] and '<img ' not in captured[0]
    assert escaped


def test_failed_email_does_not_invalidate_existing_link(reset_client,review_db,monkeypatch):
    from modules.organizations import routes_users
    client,_,_=reset_client;configure_mail(review_db);earlier=seed_link(review_db)
    monkeypatch.setattr(routes_users,'_send_odin_email',lambda *args:False)
    response=client.post('/users/1/reset-password-email',headers=admin_headers())
    assert response.status_code==503
    response=client.post('/auth/reset-password',json={'token':earlier,'new_password':new_password()})
    assert response.status_code==200


@pytest.mark.parametrize('outcome',[True,False,None,'exception'])
def test_public_forgot_password_has_same_response_for_known_and_unknown(reset_client,review_db,monkeypatch,outcome):
    from modules.organizations import routes_auth
    client,_,_=reset_client;configure_mail(review_db);calls=[]
    def send(*args):
        calls.append(True)
        if outcome=='exception':raise RuntimeError('Synthetic mail failure')
        return outcome
    monkeypatch.setattr(routes_auth,'_send_odin_email',send)
    known=client.post('/auth/forgot-password',json={'email':'student@example.test'})
    unknown=client.post('/auth/forgot-password',json={'email':'unknown@example.test'})
    assert known.status_code==unknown.status_code==200
    assert known.json()==unknown.json()
    assert calls==[True]
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM password_reset_tokens WHERE used=0')).scalar_one()==int(outcome is True)
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==2


def test_expiry_during_hashing_does_not_consume_link(reset_client,review_db,monkeypatch):
    from modules.organizations import routes_auth
    client,_,_=reset_client
    now=datetime.now(timezone.utc);token=seed_link(review_db,(now+timedelta(seconds=10)).isoformat())
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls,tz=None):return now+timedelta(minutes=1)
    original=routes_auth.hash_password
    def after_hash(password):
        hashed=original(password)
        monkeypatch.setattr(routes_auth,'datetime',FrozenDatetime)
        return hashed
    monkeypatch.setattr(routes_auth,'hash_password',after_hash)
    before=state(review_db)
    response=client.post('/auth/reset-password',json={'token':token,'new_password':new_password()})
    assert response.status_code==400
    unchanged=state(review_db)==before
    assert unchanged


def test_account_disabled_before_reset_lock_leaves_link_unused(reset_client,review_db,monkeypatch):
    client,_,_=reset_client;token=seed_link(review_db);before=state(review_db)
    original=Session.execute;changed=[]
    def disable_before_lock(db,statement,*args,**kwargs):
        if str(statement).startswith('UPDATE users SET password_hash = password_hash') and not changed:
            changed.append(True)
            with Session(review_db.get_bind()) as other:
                original(other,text('UPDATE users SET is_active=FALSE WHERE id=1'));other.commit()
        return original(db,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',disable_before_lock)
    response=client.post('/auth/reset-password',json={'token':token,'new_password':new_password()})
    assert response.status_code==400
    unchanged=state(review_db)==before
    assert unchanged
