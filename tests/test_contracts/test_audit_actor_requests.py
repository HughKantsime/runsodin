"""Actual auth and ASGI mounted against explicit024 owned fixtures."""
import json
import secrets
import pytest
from fastapi import FastAPI,Depends,Request,BackgroundTasks
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from core.audit_actor import clear_session
from core.middleware.audit_actor import AuditActorMiddleware

@pytest.fixture
def client(review_db,monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.audit import log_audit
    from core.auth import create_access_token,hash_password
    from core.audit_actor import register_request_session
    from modules.organizations.routes_auth import router
    from modules.organizations.routes_sessions import router as sessions_router
    review_db.commit()
    monkeypatch.delenv('API_KEY',raising=False)
    app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(router);app.include_router(sessions_router)
    sessions=[]
    def database(request:Request):
        with Session(review_db.get_bind(),autoflush=False) as db:
            sessions.append(db);register_request_session(db,request)
            try:yield db
            finally:clear_session(db)
    app.dependency_overrides[get_db]=database
    @app.post('/stage')
    def stage(body:dict,background:BackgroundTasks,current=Depends(get_current_user),db=Depends(get_db)):
        log_audit(db,'request',details=body,ip=body.get('ip'))
        db.commit()
        if body.get('background'):
            def later():log_audit(db,'background');db.commit()
            background.add_task(later)
        return {'known':current is not None}
    @app.get('/stream')
    def stream(current=Depends(get_current_user),db=Depends(get_db)):
        def chunks():
            log_audit(db,'stream');db.commit();yield b'owned'
        return StreamingResponse(chunks())
    @app.get('/owned-error')
    def owned_error(current=Depends(get_current_user),db=Depends(get_db)):
        raise RuntimeError('Owned request failure')
    def bearer(username):return {'Authorization':'Bearer '+create_access_token({'sub':username,'role':'admin'})}
    def token():
        raw='odin_'+secrets.token_urlsafe(32)
        review_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'owned',:h,:p,:s)"),{'h':hash_password(raw),'p':raw[:10],'s':json.dumps(['read'])});review_db.commit()
        return {'X-API-Key':raw}
    return TestClient(app,client=('198.51.100.7',50000)),bearer,token,sessions

def audit(db):
    db.expire_all()
    return db.execute(text('SELECT action,actor_user_id,actor_username,actor_auth_kind,ip_address FROM audit_logs ORDER BY id')).all()

@pytest.mark.parametrize('carrier',['bearer','cookie','token','global'])
def test_actual_verified_carriers_and_spoof_refusal(client,review_db,monkeypatch,carrier):
    c,bearer,token,_=client
    headers={}
    if carrier=='bearer':headers=bearer('admin')
    elif carrier=='cookie':c.cookies.set('session',bearer('admin')['Authorization'][7:])
    elif carrier=='token':headers=token()
    else:
        key=secrets.token_urlsafe(32);monkeypatch.setenv('API_KEY',key);headers={'X-API-Key':key}
    headers['X-Forwarded-For']='203.0.113.9'
    response=c.post('/stage',headers=headers,json={'actor_user_id':999,'username':'spoof','ip':'203.0.113.8'})
    status=response.status_code;known=response.json().get('known');assert status==200;assert known is True
    row=audit(review_db)[-1]
    if carrier=='global':assert tuple(row[1:4])==(None,None,'legacy_global_api_key')
    else:assert tuple(row[1:4])==(3,'admin',{'bearer':'bearer_jwt','cookie':'session_jwt','token':'user_api_token'}[carrier])
    assert row[4]=='198.51.100.7'

def test_final_body_background_and_next_request_unknown(client,review_db):
    c,bearer,_,sessions=client
    response=c.post('/stage',headers=bearer('admin'),json={'background':True});status=response.status_code;assert status==200
    response=c.post('/stage',json={});status=response.status_code;assert status==200
    rows=audit(review_db)
    assert [tuple(r[1:4]) for r in rows[-3:]]==[(3,'admin','bearer_jwt'),(None,None,None),(None,None,None)]
    assert rows[-2][4] is None
    assert all(not s.info for s in sessions)

def test_stream_stage_and_request_close(client,review_db):
    c,bearer,_,sessions=client
    response=c.get('/stream',headers=bearer('admin'));status=response.status_code;assert status==200
    assert tuple(audit(review_db)[-1][1:4])==(3,'admin','bearer_jwt')
    assert all(not s.info for s in sessions)

def test_verified_password_login_and_failed_login(client,review_db):
    from core.auth import hash_password
    from modules.organizations.routes_auth import limiter
    c,_,_,_=client;limiter._storage.reset()
    password=secrets.token_urlsafe(20)
    review_db.execute(text('UPDATE users SET password_hash=:h WHERE id=3'),{'h':hash_password(password)});review_db.commit()
    response=c.post('/auth/login',data={'username':'admin','password':password});status=response.status_code;assert status==200
    assert tuple(audit(review_db)[-1][1:4])==(3,'admin','password_login')
    c.cookies.clear()
    response=c.post('/auth/login',data={'username':'admin','password':'owned-wrong'});status=response.status_code;assert status==401
    assert tuple(audit(review_db)[-1][1:4])==(None,None,None)

def test_partial_then_verified_mfa_login(client,review_db,monkeypatch):
    import pyotp
    from cryptography.fernet import Fernet
    from core.crypto import encrypt
    from core.auth import hash_password
    from modules.organizations.routes_auth import limiter
    c,_,_,_=client;limiter._storage.reset()
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    secret=pyotp.random_base32();password=secrets.token_urlsafe(20)
    review_db.execute(text('UPDATE users SET password_hash=:h,mfa_enabled=1,mfa_secret=:s WHERE id=3'),{'h':hash_password(password),'s':encrypt(secret)});review_db.commit()
    response=c.post('/auth/login',data={'username':'admin','password':password});status=response.status_code;assert status==200
    challenge=response.json()['access_token']
    assert not [r for r in audit(review_db) if r[0]=='auth.login']
    response=c.post('/stage',headers={'Authorization':'Bearer '+challenge},json={});status=response.status_code;assert status==200
    assert tuple(audit(review_db)[-1][1:4])==(None,None,None)
    response=c.post('/auth/mfa/verify',json={'mfa_token':challenge,'code':pyotp.TOTP(secret).now()});status=response.status_code;assert status==200
    assert tuple(audit(review_db)[-1][1:4])==(3,'admin','mfa_login')


def test_concurrent_requests_keep_exact_pair(client,review_db):
    from concurrent.futures import ThreadPoolExecutor
    c,bearer,_,_=client
    def call(name):
        response=c.post('/stage',headers=bearer(name),json={'marker':name})
        return response.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(call,['admin','student']))
    assert statuses==[200,200]
    review_db.expire_all()
    rows=review_db.execute(text("SELECT actor_username,details FROM audit_logs WHERE action='request'")).all()
    assert len(rows)==2
    assert all(name==json.loads(details)['marker'] for name,details in rows)

def test_error_scope_and_session_cleared(client):
    c,bearer,_,sessions=client
    with pytest.raises(RuntimeError,match='Owned request failure'):
        c.get('/owned-error',headers=bearer('admin'))
    assert all(not s.info for s in sessions)

def test_asgi_disconnect_clears_before_downstream_receive():
    import asyncio
    from core.audit_actor import register_request_session,stage_actor
    from core.models import AuditLog
    sessions=[]
    async def app(scope,receive,send):
        db=Session();sessions.append(db)
        register_request_session(db,Request(scope))
        from core.audit_actor import bind_verified_actor
        bind_verified_actor(db,{'id':1,'username':'owned','_auth_kind':'session_jwt'})
        assert (await receive())['type']=='http.disconnect'
        item=AuditLog(action='later');stage_actor(db,item)
        assert item._odin_staged_actor[1] is None
        assert not db.info
    async def receive():return {'type':'http.disconnect'}
    async def send(message):pass
    asyncio.run(AuditActorMiddleware(app)({'type':'http','client':('198.51.100.7',1)},receive,send))
    assert all(not s.info for s in sessions)


def test_actual_get_db_shared_request_binding_and_final_clear(client,review_db,monkeypatch):
    from core.db import get_db
    c,bearer,_,_=client
    received=[]
    def factory():
        db=Session(review_db.get_bind(),autoflush=False);received.append(db);return db
    monkeypatch.setattr('core.db.SessionLocal',factory)
    c.app.dependency_overrides.pop(get_db)
    response=c.post('/stage',headers=bearer('admin'),json={});status=response.status_code;assert status==200
    assert tuple(audit(review_db)[-1][1:4])==(3,'admin','bearer_jwt')
    assert len(received)==1 and not received[0].info

def test_registered_bootstrap_repeat_keeps_baseline_and024(review_db):
    from core.schema import bootstrap_database
    from core.schema.bootstrap import _orm_checksum
    bind=review_db.get_bind();review_db.commit()
    before=_orm_checksum()
    result=bootstrap_database(bind)
    assert 'python:024-audit-actor' not in result['applied']
    with bind.connect() as c:
        assert c.execute(text("SELECT count(*) FROM odin_schema_migrations WHERE migration_id='python:024-audit-actor'")).scalar_one()==1
    assert _orm_checksum()==before

def test_actual_gdpr_http_sweeps_known_actor_and_preserves_other_actor(client,review_db):
    c,bearer,_,_=client
    response=c.post('/stage',headers=bearer('student'),json={});status=response.status_code;assert status==200
    response=c.post('/stage',headers=bearer('admin'),json={});status=response.status_code;assert status==200
    response=c.delete('/users/1/erase',headers=bearer('admin'));status=response.status_code;assert status==200
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT count(*) FROM audit_actor_erasures WHERE user_id=1')).scalar_one()==1
        rows=db.execute(text("SELECT actor_user_id,actor_username,actor_auth_kind,ip_address FROM audit_logs WHERE action='request' ORDER BY id")).all()
        assert tuple(rows[0])==(None,None,None,None)
        assert tuple(rows[1])==(3,'admin','bearer_jwt','198.51.100.7')
        final=db.execute(text("SELECT actor_user_id,actor_username,actor_auth_kind FROM audit_logs WHERE action='gdpr_erasure'")).one()
        assert tuple(final)==(3,'admin','bearer_jwt')

def test_actual_self_erasure_final_audit_and_id_reuse_unknown(client,review_db):
    c,bearer,_,_=client
    response=c.post('/stage',headers=bearer('admin'),json={});status=response.status_code;assert status==200
    response=c.delete('/users/3/erase',headers=bearer('admin'));status=response.status_code;assert status==200
    with Session(review_db.get_bind()) as db:
        rows=db.execute(text("SELECT actor_user_id,actor_username,actor_auth_kind,ip_address,details FROM audit_logs WHERE action='gdpr_erasure'")).all()
        assert len(rows)==1 and tuple(rows[0][:4])==(None,None,None,None)
        assert 'actor_user_id' not in json.loads(rows[0][4])
        assert db.execute(text('SELECT count(*) FROM audit_actor_erasures WHERE user_id=3')).scalar_one()==1
        # Physical delete is optional and constrained by unrelated FK rows; numeric reuse
        # is modeled by the real users row becoming a new active account at the sameID.
        db.execute(text("UPDATE users SET username='reused-account',is_active=1,password_hash='fixture' WHERE id=3"));db.commit()
    response=c.post('/stage',headers=bearer('reused-account'),json={});status=response.status_code;assert status==200
    assert tuple(audit(review_db)[-1][1:])==(None,None,None,None)

def test_gdpr_marker_or_stamp_failure_rolls_back_http_mutation(client,review_db,monkeypatch):
    c,bearer,_,_=client
    original=Session.execute
    def refuse(self,statement,*args,**kwargs):
        if str(statement).startswith('INSERT INTO audit_actor_erasures'):raise RuntimeError('Owned marker refusal')
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',refuse)
    with pytest.raises(RuntimeError,match='Owned marker refusal'):
        c.delete('/users/1/erase',headers=bearer('admin'))
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT username,is_active FROM users WHERE id=1')).one()==('student',1)
        assert db.execute(text('SELECT count(*) FROM audit_actor_erasures')).scalar_one()==0
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='gdpr_erasure'")).scalar_one()==0

def test_personal_export_excludes_other_recorded_actor_fields(client,review_db):
    c,bearer,_,_=client
    # Actual authenticated mutation attributes admin to an event involving student.
    response=c.post('/stage',headers=bearer('admin'),json={});status=response.status_code;assert status==200
    with Session(review_db.get_bind()) as db:
        db.execute(text("UPDATE audit_logs SET entity_type='user',entity_id=1 WHERE action='request'"));db.commit()
    response=c.get('/users/1/export',headers=bearer('student'));status=response.status_code;assert status==200
    events=response.json()['audit_log_entries'];assert events
    assert all(not {'actor_user_id','actor_username','actor_auth_kind'}&set(row) for row in events)
    with Session(review_db.get_bind()) as db:
        assert db.execute(text("SELECT actor_username FROM audit_logs WHERE action='request'")).scalar_one()=='admin'

def test_real_create_app_installs_private_binding(review_db,monkeypatch):
    from core import app as app_module
    from core.auth import create_access_token
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.audit import log_audit
    from core.config import settings
    # Actual factory/middleware stack; omit unrelated module discovery and lifespan
    # so this fixture starts no monitor/network/hardware/background services.
    monkeypatch.setattr(app_module,'_discover_modules',lambda:[])
    monkeypatch.setattr(settings,'trusted_hosts','*')
    monkeypatch.setattr(settings,'api_key',None)
    monkeypatch.setattr('core.db.SessionLocal',lambda:Session(review_db.get_bind(),autoflush=False))
    app=app_module.create_app()
    @app.post('/api/owned-actor-factory')
    def probe(current=Depends(get_current_user),db=Depends(get_db)):
        log_audit(db,'real-factory');db.commit();return {'known':current is not None}
    token=create_access_token({'sub':'admin','role':'admin'})
    client=TestClient(app,client=('198.51.100.7',50000))
    response=client.post('/api/owned-actor-factory',headers={'Authorization':'Bearer '+token},json={})
    status=response.status_code;assert status==200
    assert tuple(audit(review_db)[-1][1:])==(3,'admin','bearer_jwt','198.51.100.7')

def test_gdpr_final_actor_stamp_failure_restores_marker_user_and_history(client,review_db,monkeypatch):
    c,bearer,_,_=client
    response=c.post('/stage',headers=bearer('student'),json={});status=response.status_code;assert status==200
    def refuse(connection,uid):raise RuntimeError('Owned final actor stamp refusal')
    monkeypatch.setattr('core.audit_actor._erased',refuse)
    with pytest.raises(RuntimeError,match='Owned final actor stamp refusal'):
        c.delete('/users/1/erase',headers=bearer('admin'))
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT username,is_active FROM users WHERE id=1')).one()==('student',1)
        assert db.execute(text('SELECT count(*) FROM audit_actor_erasures')).scalar_one()==0
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='gdpr_erasure'")).scalar_one()==0
        assert db.execute(text("SELECT actor_user_id,actor_username,actor_auth_kind,ip_address FROM audit_logs WHERE action='request'")).one()==(1,'student','bearer_jwt','198.51.100.7')

def test_http_erasure_marker_outlives_physical_user_delete(client,review_db):
    c,bearer,_,_=client
    review_db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(80,'owned-isolated','fixture','viewer',1,1)"));review_db.commit()
    response=c.post('/stage',headers=bearer('owned-isolated'),json={});status=response.status_code;assert status==200
    response=c.delete('/users/80/erase',headers=bearer('admin'));status=response.status_code;assert status==200
    with Session(review_db.get_bind()) as db:
        db.execute(text('DELETE FROM users WHERE id=80'));db.commit()
        assert db.execute(text('SELECT count(*) FROM audit_actor_erasures WHERE user_id=80')).scalar_one()==1
        db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(80,'owned-new-account','fixture','viewer',1,1)"));db.commit()
    response=c.post('/stage',headers=bearer('owned-new-account'),json={});status=response.status_code;assert status==200
    assert tuple(audit(review_db)[-1][1:])==(None,None,None,None)
