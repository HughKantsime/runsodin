"""Actual camera/timelapse HTTP scopes, no physical camera or transcoder."""
import pytest
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import Request
from tests.test_contracts.test_timelapse_lifetime_membership import prepared_source,encoder,frame_writer,owned_runtime
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

def attach_plain_camera_owner(db,tid=199):
    """Explicit plain-content capture fixture, not a legacy owner backfill."""
    from core.timelapse_ownership_schema import anchors
    db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(668,'owned.3mf','owned.3mf','/owned/camera.3mf',1)"))
    db.execute(text("INSERT INTO jobs(id,item_name,printer_id,print_file_id,charged_to_org_id,status) VALUES(667,'Owned camera job',9,668,1,'completed')"))
    filename=db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    nonce='owned-camera-capture'
    db.execute(anchors.insert(),{'operation_id':nonce,'timelapse_id':tid,'printer_id':9,'scheduled_job_id':667,'filename':filename,'owner_state':'known','owner_org_id':1,'ownership_source':'first_capture_job_file','print_file_id':668,'capture_printer_org_id':1,'capture_printer_shared':False})
    db.execute(text('UPDATE timelapses SET print_job_id=667,ownership_operation_id=:nonce WHERE id=:id'),{'nonce':nonce,'id':tid})
    db.execute(text('UPDATE printers SET shared=0 WHERE id=9'))
    db.commit()

@pytest.fixture
def camera_client(token_client,review_db,monkeypatch):
    from modules.printers import camera_routes
    client,issue=token_client
    from core.registry import registry
    from modules.archives import register_providers
    monkeypatch.setattr(registry,'_providers',dict(registry._providers))
    register_providers(registry)
    from core.middleware.audit_actor import AuditActorMiddleware
    from core.audit_actor import register_request_session,clear_session
    from core.db import get_db
    client.app.add_middleware(AuditActorMiddleware)
    def database(request:Request):
        with Session(review_db.get_bind(),autoflush=False) as db:
            register_request_session(db,request)
            try:yield db
            finally:clear_session(db)
    client.app.dependency_overrides[get_db]=database
    client.app.include_router(camera_routes.router)
    calls=[]
    monkeypatch.setattr(camera_routes,'sync_go2rtc_config',lambda db:calls.append('sync'))
    monkeypatch.setattr(camera_routes,'get_camera_url',lambda printer:'rtsp://synthetic.invalid/camera')
    review_db.execute(text('UPDATE printers SET camera_enabled=1,is_active=1'))
    review_db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status,frame_count) VALUES(199,9,'synthetic.mp4','ready',3)"));review_db.commit()
    attach_plain_camera_owner(review_db)
    return client,issue,calls

@pytest.fixture
def managed_camera_client(camera_client,prepared_source,monkeypatch):
    """Real receipt-owned publication; encoder payload is explicitly synthetic."""
    from modules.printers import camera_routes
    client,issue,calls=camera_client
    engine,root,tid,_,nonce=prepared_source
    monkeypatch.setattr(camera_routes,'TIMELAPSE_ROOT',str(root))
    with Session(engine) as db:filename=db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    return client,issue,calls,tid,nonce,(root/filename).read_bytes(),engine

@pytest.mark.parametrize('path',['/cameras','/cameras/9/stream','/timelapses','/timelapses/199'])
def test_unrelated_token_cannot_read_camera_or_timelapse(camera_client,path):
    client,issue,calls=camera_client
    response=client.get(path,headers=issue(['write:jobs']))
    assert response.status_code==403
    assert calls==[]

@pytest.mark.parametrize('scope',['read:printers','write:jobs','read'])
def test_narrow_token_cannot_toggle_camera(camera_client,review_db,scope):
    client,issue,calls=camera_client
    response=client.patch('/cameras/9/toggle',headers=issue([scope]))
    assert response.status_code==403
    assert review_db.execute(text('SELECT camera_enabled FROM printers WHERE id=9')).scalar_one()==1
    assert calls==[]

@pytest.mark.parametrize('scope',['read:printers','read','admin'])
@pytest.mark.parametrize('path',['/cameras','/cameras/9/stream','/timelapses','/timelapses/199'])
def test_matching_read_scopes_keep_camera_metadata_usable(camera_client,path,scope):
    client,issue,_=camera_client
    assert client.get(path,headers=issue([scope])).status_code==200

@pytest.mark.parametrize('operation',['trim','speed'])
@pytest.mark.parametrize('scope',['read:printers','write:jobs'])
def test_narrow_token_cannot_start_media_editor(camera_client,scope,operation,monkeypatch):
    from modules.printers import camera_routes
    client,issue,calls=camera_client
    from modules.archives import timelapse_edit
    monkeypatch.setattr(timelapse_edit,'execute_edit',lambda *a,**k:pytest.fail('unrelated scope must not enter filesystem/process service'))
    response=client.post(f'/timelapses/199/{operation}',headers=issue([scope]),json={'start_seconds':0,'end_seconds':1,'multiplier':2})
    assert response.status_code==403 and calls==[]

@pytest.mark.parametrize('field',['camera_enabled','is_active'])
def test_disabled_or_inactive_camera_has_no_direct_stream(camera_client,review_db,field):
    client,issue,calls=camera_client
    review_db.execute(text(f'UPDATE printers SET {field}=0 WHERE id=9'));review_db.commit()
    response=client.get('/cameras/9/stream',headers=issue(['read:printers']))
    assert response.status_code==404 and calls==[]

@pytest.mark.parametrize('operation,body',[
 ('trim',{'start_seconds':'bad','end_seconds':2}),
 ('trim',{'start_seconds':{},'end_seconds':2}),
 ('trim',{'start_seconds':True,'end_seconds':2}),
 ('trim',{'start_seconds':0,'end_seconds':None}),
 ('trim',{'start_seconds':2,'end_seconds':1}),
 ('trim',{'start_seconds':0,'end_seconds':10**400}),
 ('speed',{'multiplier':10**400}),
 ('speed',{'multiplier':'bad'}),('speed',{'multiplier':{}}),('speed',{'multiplier':True}),('speed',{'multiplier':None}),
])
def test_malformed_media_edit_returns_client_error_before_process(managed_camera_client,operation,body,monkeypatch):
    from modules.printers import camera_routes
    from modules.archives import timelapse_edit
    client,issue,calls,tid,nonce,expected,engine=managed_camera_client
    monkeypatch.setattr(timelapse_edit,'execute_edit',lambda *a,**k:pytest.fail('invalid recipe must not enter filesystem/process service'))
    headers={**issue(['write:printers']),'If-Match':'"'+nonce+'"'}
    response=client.post(f'/timelapses/{tid}/{operation}',headers=headers,json={'operation_id':'a'*32,**body})
    status=response.status_code;assert status==400
    with Session(engine) as db:
        assert db.execute(text("SELECT count(*) FROM timelapse_publications WHERE kind IN ('trim','speed')")).scalar_one()==0
        filename=db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    assert (Path(camera_routes.TIMELAPSE_ROOT)/filename).read_bytes()==expected and calls==[]

@pytest.mark.parametrize('path',['/timelapses/199/video','/timelapses/199/stream','/timelapses/199/download'])
def test_unrelated_token_cannot_resolve_private_media(camera_client,path,monkeypatch):
    from modules.printers import camera_routes
    client,issue,calls=camera_client
    from modules.archives import timelapse_read_lease
    monkeypatch.setattr(timelapse_read_lease,'admitted_response',lambda *a,**k:pytest.fail('unrelated token must not enter filesystem admission'))
    # All managed read routes must deny unrelated scopes before FD admission.
    assert client.get(path,headers=issue(['write:jobs'])).status_code==403


def test_read_only_token_cannot_delete_timelapse_record(camera_client,review_db):
    client,issue,_=camera_client
    response=client.delete('/timelapses/199',headers=issue(['read:printers']))
    assert response.status_code==403
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapses WHERE id=199')).scalar_one()==1


def test_missing_printer_fails_closed_for_timelapse_detail(camera_client,review_db):
    client,issue,_=camera_client
    # Simulate an orphan retained recording in a legacy import, without weakening the schema.
    from modules.printers import camera_routes
    class OrphanQuery:
        def filter(self,*a):return self
        def first(self):return None
    # The real recording exists; only the missing parent query is modeled at the parent boundary.
    from sqlalchemy.orm import Session
    original_query=Session.query
    def query(self,entity,*args,**kwargs):
        if entity is camera_routes.Printer:return OrphanQuery()
        return original_query(self,entity,*args,**kwargs)
    from unittest.mock import patch
    with patch.object(Session,'query',query):
        response=client.get('/timelapses/199',headers=issue(['read:printers']))
    assert response.status_code==404

@pytest.mark.parametrize('path',['stream','download'])
def test_matching_token_serves_owned_media_bytes(managed_camera_client,path):
    client,issue,_,tid,_,expected,_=managed_camera_client
    response=client.get(f'/timelapses/{tid}/{path}',headers=issue(['read:printers']))
    status=response.status_code;assert status==200
    assert response.content==expected and response.headers['content-type']=='video/mp4'
    if path=='download':assert f'odin_timelapse_{tid}.mp4' in response.headers['content-disposition']

@pytest.mark.parametrize('operation,body',[('trim',{'start_seconds':0,'end_seconds':1}),('speed',{'multiplier':2})])
def test_authorized_missing_if_match_is428_before_fs(managed_camera_client,operation,body,monkeypatch):
    from modules.archives import timelapse_edit
    client,issue,_,tid,_,_,engine=managed_camera_client
    monkeypatch.setattr(timelapse_edit,'execute_edit',lambda *a,**k:pytest.fail('missing precondition must not enter filesystem/process service'))
    response=client.post(f'/timelapses/{tid}/{operation}',headers=issue(['write:printers']),json={'operation_id':'a'*32,**body})
    status=response.status_code;assert status==428
    with Session(engine) as db:assert db.execute(text("SELECT count(*) FROM timelapse_publications WHERE kind IN ('trim','speed')")).scalar_one()==0

@pytest.mark.parametrize('path',['stream','download'])
def test_matching_token_owned_head_and_range(managed_camera_client,path):
    client,issue,_,tid,_,expected,_=managed_camera_client
    headers=issue(['read:printers'])
    response=client.head(f'/timelapses/{tid}/{path}',headers=headers)
    status=response.status_code;assert status==200
    assert response.content==b'' and int(response.headers['content-length'])==len(expected)
    response=client.get(f'/timelapses/{tid}/{path}',headers={**headers,'Range':'bytes=1-3'})
    status=response.status_code;assert status==206
    assert response.content==expected[1:4] and response.headers['content-range']==f'bytes 1-3/{len(expected)}'

@pytest.mark.parametrize('scope',['write:printers','write','admin'])
def test_authorized_camera_toggle_persists(camera_client,review_db,scope):
    client,issue,_=camera_client
    response=client.patch('/cameras/9/toggle',headers=issue([scope]))
    assert response.status_code==200 and response.json()['camera_enabled'] is False
    assert review_db.execute(text('SELECT camera_enabled FROM printers WHERE id=9')).scalar_one()==0

@pytest.mark.parametrize('field',['camera_enabled','is_active'])
def test_disabled_or_inactive_camera_never_proxies_webrtc(camera_client,review_db,field,monkeypatch):
    from modules.printers import camera_routes
    client,issue,calls=camera_client
    review_db.execute(text(f'UPDATE printers SET {field}=0 WHERE id=9'));review_db.commit()
    monkeypatch.setattr(camera_routes.httpx,'AsyncClient',lambda *a,**k:pytest.fail('disabled camera must never contact signaling peer'))
    response=client.post('/cameras/9/webrtc',headers=issue(['read:printers']),content='SYNTHETIC_SDP')
    assert response.status_code==404 and calls==[]
