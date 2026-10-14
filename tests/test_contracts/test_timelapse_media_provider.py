"""Real media owner boundary, without changing its authority or I/O behavior."""
import ast,importlib
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from core.registry import registry,ModuleRegistry
from tests.test_contracts.test_timelapse_content_audience import media_app,managed_plain_media,review_db,owned_runtime


def test_same_exception_objects_and_exact_original_definitions():
    from core import timelapse_media_errors as core
    for module,name in [('timelapse_retirement','RetirementUnavailable'),('timelapse_edit','EditUnavailable'),('timelapse_lifetime_action','NoCompletedReceiptYet')]:
        owner=importlib.import_module('modules.archives.'+module)
        assert getattr(owner,name) is getattr(core,name)
        before=Path('tests/fixtures/certification/timelapse-media-provider/backend_modules_archives_'+module+'.py.before').read_text()
        old=next(n for n in ast.parse(before).body if isinstance(n,ast.ClassDef) and n.name==name)
        now=Path(core.__file__).read_text();new=next(n for n in ast.parse(now).body if isinstance(n,ast.ClassDef) and n.name==name)
        assert ast.get_source_segment(before,old)==ast.get_source_segment(now,new)
    error=core.RetirementUnavailable('owned');assert error.reason=='owned' and str(error)=='Retirement preparation could not be confirmed'
    assert issubclass(core.EditUnavailable,ValueError)


@pytest.mark.parametrize('name,module,args,kwargs',[
 ('capture_action_credential','timelapse_action_authority',3,{'kind':'kind','carrier':'carrier','purpose':'read','selected':object()}),
 ('admitted_response','timelapse_read_lease',4,{'range_header':'bytes=0-1','head':True,'filename':'owned.mp4','expected_nonce':'nonce'}),
 ('sealed_lifetime_receipt','timelapse_lifetime_action',2,{'operation_id':'nonce','original_id':7}),
 ('prepare_request_lifetime_retirement','timelapse_lifetime_prepare',4,{'expected_nonce':'nonce','budget':object()}),
 ('retire_sealed_lifetime','timelapse_lifetime_action',4,{'expected_nonce':'nonce','budget':object()}),
 ('execute_edit','timelapse_edit',5,{'nonce':'nonce','recipe':object(),'lookup':False}),
])
def test_exact_delegate_identity_and_exception(name,module,args,kwargs,monkeypatch):
    from modules.archives.timelapse_media_provider import TimelapseMediaService
    owner=importlib.import_module('modules.archives.'+module);values=tuple(object() for _ in range(args));result=object();seen=[]
    def delegate(*actual,**options):
        assert all(a is b for a,b in zip(actual,values)) and len(actual)==len(values)
        assert options.keys()==kwargs.keys() and all(options[k] is kwargs[k] for k in kwargs)
        seen.append(True);return result
    monkeypatch.setattr(owner,name,delegate)
    assert getattr(TimelapseMediaService(),name)(*values,**kwargs) is result
    error=HTTPException(412,'owned')
    def failed(*a,**k):raise error
    monkeypatch.setattr(owner,name,failed)
    with pytest.raises(HTTPException) as caught:getattr(TimelapseMediaService(),name)(*values,**kwargs)
    assert caught.value is error and seen==[True]


def test_retirement_authorization_preserves_order_and_caller_session(monkeypatch):
    from modules.archives.timelapse_media_provider import TimelapseMediaService
    from modules.archives import timelapse_action_authority
    import core.timelapse_access as access
    evidence=object();actor=object();media=SimpleNamespace(ownership_operation_id='nonce');calls=[]
    class SessionSentinel:
        def get(self,model,identifier):calls.append('get');assert identifier==7;return media
    db=SessionSentinel()
    def fence(given):assert given is db;calls.append('fence')
    def principal(given,proof):assert given is db and proof is evidence;calls.append('principal');return actor
    def check(given,row,who,**kwargs):assert given is db and row is media and who is actor and kwargs=={'mutation':True};calls.append('access')
    monkeypatch.setattr(timelapse_action_authority,'fence_authorization',fence);monkeypatch.setattr(timelapse_action_authority,'fresh_action_principal',principal);monkeypatch.setattr(access,'assert_timelapse_access',check)
    assert TimelapseMediaService().authorize_retirement(db,evidence,7,'nonce') is None
    assert calls==['fence','principal','get','access']
    with pytest.raises(HTTPException) as caught:TimelapseMediaService().authorize_retirement(db,evidence,7,'stale')
    assert caught.value.status_code==412


def test_registration_no_partial_overwrite_or_io(monkeypatch):
    from modules.archives import register_providers
    from modules.archives.attempt_valuation_provider import AttemptValuationService
    from modules.archives.timelapse_media_provider import TimelapseMediaService
    from core.interfaces.timelapse_media import MEDIA_METHODS
    for cls,methods in [(AttemptValuationService,('read_private_valuation',)),(TimelapseMediaService,MEDIA_METHODS)]:
        for method in methods:monkeypatch.setattr(cls,method,lambda *a,**k:pytest.fail('registration must not invoke I/O/authority'))
    r=ModuleRegistry();r.register_provider('TimelapseMediaProvider',object())
    with pytest.raises(TypeError):register_providers(r)
    assert not r.has_provider('AttemptValuationProvider')
    r=ModuleRegistry();register_providers(r);old=dict(r._providers);register_providers(r)
    assert all(r._providers[k] is old[k] for k in old)


@pytest.mark.parametrize('operation',['read','delete','edit','lookup'])
@pytest.mark.parametrize('bad',[None,object()])
def test_real_authenticated_missing_service_zero_effects(media_app,managed_plain_media,review_db,monkeypatch,operation,bad):
    from modules.archives import timelapse_action_authority
    client,current,_,_=media_app;tid=managed_plain_media
    current['principal'].update(id=5,group_id=2)
    detail=client.get(f'/timelapses/{tid}');nonce=detail.headers['etag']
    from tests.test_contracts.test_timelapse_api_containment import snapshot
    from modules.printers import camera_routes
    root=Path(camera_routes.TIMELAPSE_ROOT)
    before=snapshot(review_db)
    files={str(p.relative_to(root)):(p.stat().st_ino,p.read_bytes()) for p in root.rglob('*') if p.is_file()}
    monkeypatch.setitem(registry._providers,'TimelapseMediaProvider',bad)
    monkeypatch.setattr(timelapse_action_authority,'capture_action_credential',lambda *a,**k:pytest.fail('Missing service must precede credential mint'))
    headers={'If-Match':nonce}
    if operation=='read':result=client.get(f'/timelapses/{tid}/download')
    elif operation=='delete':result=client.delete(f'/timelapses/{tid}',headers=headers)
    elif operation=='edit':result=client.post(f'/timelapses/{tid}/speed',headers=headers,json={'operation_id':'a'*32,'multiplier':2})
    else:result=client.get(f'/timelapses/{tid}/edits/'+('a'*32))
    assert result.status_code==503 and result.headers['cache-control']=='private, no-store'
    assert snapshot(review_db)==before
    assert {str(p.relative_to(root)):(p.stat().st_ino,p.read_bytes()) for p in root.rglob('*') if p.is_file()}==files


def test_retained_provider_survives_registry_change_after_real_mint(media_app,managed_plain_media,monkeypatch):
    client,current,_,_=media_app;current['principal'].update(id=5,group_id=2)
    provider=registry.get_provider('TimelapseMediaProvider');original=provider.capture_action_credential;minted=[]
    def mint(*args,**kwargs):
        evidence=original(*args,**kwargs);minted.append(True)
        monkeypatch.setitem(registry._providers,'TimelapseMediaProvider',object())
        return evidence
    monkeypatch.setattr(provider,'capture_action_credential',mint)
    result=client.get(f'/timelapses/{managed_plain_media}/download')
    assert result.status_code==200 and result.content==b'owned synthetic MP4 publication evidence'
    assert minted==[True]


@pytest.mark.parametrize('method,path,range_header,status',[('GET','download',None,200),('HEAD','video',None,200),('GET','stream','bytes=0-3',206),('GET','download','bytes=999999-',416)])
def test_actual_api_security_middleware_preserves_media_cache_headers(media_app,managed_plain_media,review_db,monkeypatch,method,path,range_header,status):
    from core.app import _register_http_middleware
    from modules.printers import camera_routes
    from sqlalchemy.orm import Session
    client,current,_,_=media_app;current['principal'].update(id=5,group_id=2)
    monkeypatch.setattr('core.db.SessionLocal',lambda:Session(review_db.get_bind()))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware.audit_actor import AuditActorMiddleware
    from core.db import get_db
    app=FastAPI();app.add_middleware(AuditActorMiddleware)
    app.include_router(camera_routes.router,prefix='/api')
    app.dependency_overrides[get_db]=client.app.dependency_overrides[get_db]
    _register_http_middleware(app)
    from modules.organizations.routes_auth import router as auth_router
    from core.auth import hash_password
    from core.config import settings
    from sqlalchemy import text
    monkeypatch.setattr(settings,'api_key','owned-synthetic-perimeter-key')
    app.include_router(auth_router,prefix='/api')
    review_db.execute(text('UPDATE users SET password_hash=:hash WHERE id=5'),{'hash':hash_password('owned-synthetic-password')});review_db.commit()
    client=TestClient(app,client=('127.0.0.1',50123))
    login=client.post('/api/auth/login',data={'username':'foreign','password':'owned-synthetic-password'})
    assert login.status_code==200
    assert bool(client.cookies.get('session'))
    headers={}
    if range_header is not None:headers['Range']=range_header
    result=client.request(method,f'/api/timelapses/{managed_plain_media}/{path}',headers=headers)
    assert result.status_code==status,result.text
    assert result.headers['cache-control']=='no-store' and result.headers['pragma']=='no-cache'
    if status==206:assert result.headers['content-range'].startswith('bytes 0-3/')
    if method=='HEAD':assert result.content==b''


def test_real_all_module_factory_registration_and_zero_io(tmp_path):
    import os,subprocess,sys,json
    root=Path(__file__).resolve().parents[2]
    script='''
import json,os,socket,subprocess,sqlite3
from sqlalchemy.engine import Engine
from fastapi import FastAPI
from core.app import create_app
from core.registry import registry
import modules.archives as archives
from unittest.mock import patch
seen=[]
real=archives.register_providers
def forbidden(*args,**kwargs):raise AssertionError('registration attempted I/O')
def guarded(r):
 with patch.object(Engine,'connect',forbidden),patch.object(sqlite3,'connect',forbidden),patch.object(os,'open',forbidden),patch.object(socket,'socket',forbidden),patch.object(subprocess,'Popen',forbidden):real(r)
 seen.append('registered')
archives.register_providers=guarded
include=FastAPI.include_router
checks=[]
def observed(self,router,*args,**kwargs):
 if any(getattr(x,'path','')=='/timelapses/{timelapse_id}/video' for x in router.routes):
  assert registry.has_provider('TimelapseMediaProvider');checks.append('printer')
 if any(getattr(x,'path','')=='/orders/attempts/{attempt_id}/unit-valuation' for x in router.routes):
  assert registry.has_provider('AttemptValuationProvider');checks.append('orders')
 return include(self,router,*args,**kwargs)
FastAPI.include_router=observed
app=create_app()
assert seen==['registered'] and checks.count('printer')==2 and checks.count('orders')==2
assert any(getattr(x,'path','')=='/api/timelapses/{timelapse_id}/video' for x in app.routes)
print(json.dumps({'registration':seen,'route_checks':checks,'factory_routes':len(app.routes)}))
'''
    env=dict(os.environ,DATABASE_URL='sqlite:///'+str(tmp_path/'owned-factory.db'),PYTHONPATH=str(root/'backend'),ODIN_CONTAINER_EXEC='false',BASE_URL='http://127.0.0.1:9',DEBUG='false',COOKIE_SECURE='false')
    result=subprocess.run([sys.executable,'-c',script],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr[-1000:]
    assert json.loads(result.stdout.splitlines()[-1])['registration']==['registered']


def test_configured_global_key_perimeter_has_explicit_bearer_only_refusal(media_app,managed_plain_media,review_db,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.app import _register_http_middleware
    from core.config import settings
    from core.db import get_db
    from core.auth import create_access_token
    from core.middleware.audit_actor import AuditActorMiddleware
    from modules.printers import camera_routes
    from sqlalchemy.orm import Session
    client,_,_,_=media_app
    monkeypatch.setattr(settings,'api_key','owned-synthetic-perimeter-key')
    monkeypatch.setattr('core.db.SessionLocal',lambda:Session(review_db.get_bind()))
    app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(camera_routes.router,prefix='/api')
    app.dependency_overrides[get_db]=client.app.dependency_overrides[get_db]
    _register_http_middleware(app)
    result=TestClient(app).get(f'/api/timelapses/{managed_plain_media}/download',headers={'Authorization':'Bearer '+create_access_token({'sub':'foreign'})})
    assert result.status_code==401 and result.headers['cache-control']=='no-store'
