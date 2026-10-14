"""Actual JWT/token HTTP with real SQLite publication; codec seam synthetic."""
import secrets
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import text
from core.db import get_db
from core.auth import create_access_token,hash_password
from core.middleware.audit_actor import AuditActorMiddleware
from tests.test_contracts.test_timelapse_edit_publication import edit,prepared_source,encoder,frame_writer,review_db,owned_runtime

@pytest.fixture
def http_edit(edit,monkeypatch):
    from modules.printers import camera_routes
    service,engine,root,tid,nonce,_=edit
    monkeypatch.setattr(camera_routes,'TIMELAPSE_ROOT',str(root))
    app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(camera_routes.router)
    def database():
        with Session(engine) as db:yield db
    app.dependency_overrides[get_db]=database
    with Session(engine) as db:username=db.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()
    token=create_access_token({'sub':username,'role':'admin'})
    with TestClient(app) as client:yield client,engine,tid,nonce,token

def test_real_jwt_trim_replay_and_superseded_version_read412(http_edit):
    client,engine,tid,nonce,token=http_edit
    headers={'Authorization':'Bearer '+token,'If-Match':'"'+nonce+'"'}
    body={'operation_id':'a'*32,'start_seconds':0,'end_seconds':.09}
    response=client.post(f'/timelapses/{tid}/trim',headers=headers,json=body)
    status=response.status_code;assert status==200
    first=response.json()
    response=client.post(f'/timelapses/{tid}/trim',headers=headers,json=body)
    status=response.status_code;assert status==200
    assert response.json()==first
    response=client.post(f'/timelapses/{tid}/trim',headers={**headers,'If-Match':'"'+first['result_nonce']+'"'},json={**body,'operation_id':'b'*32,'end_seconds':.05})
    status=response.status_code;assert status==200
    response=client.get(f'/timelapses/{tid}/edits/'+ 'a'*32,headers={'Authorization':'Bearer '+token})
    status=response.status_code;assert status==200
    assert not response.json()['playback_available']
    response=client.get(f'/timelapses/{tid}/stream',params={'version':'"'+first['result_nonce']+'"'},headers={'Authorization':'Bearer '+token})
    status=response.status_code;assert status==412

def test_api_token_actual_scope_and_private_operation_lookup(http_edit):
    client,engine,tid,nonce,_=http_edit
    key='odin_'+secrets.token_hex(24)
    with engine.begin() as c:
        c.execute(text("INSERT INTO api_tokens(name,user_id,token_prefix,token_hash,scopes) VALUES('owned-editor',3,:prefix,:hash,:scopes)"),
            {'prefix':key[:10],'hash':hash_password(key),'scopes':'["read:printers"]'})
    body={'operation_id':'a'*32,'multiplier':2}
    headers={'X-API-Key':key,'If-Match':'"'+nonce+'"'}
    response=client.post(f'/timelapses/{tid}/speed',headers=headers,json=body)
    status=response.status_code;assert status==403
    with engine.begin() as c:c.execute(text("UPDATE api_tokens SET scopes='[\"write:printers\"]' WHERE token_prefix=:prefix"),{'prefix':key[:10]})
    response=client.post(f'/timelapses/{tid}/speed',headers=headers,json=body)
    status=response.status_code;assert status==200
    response=client.get(f'/timelapses/{tid}/edits/'+ 'a'*32,headers={'X-API-Key':key})
    status=response.status_code;assert status==200
    with engine.begin() as c:c.execute(text("UPDATE api_tokens SET scopes='[\"read:printers\"]' WHERE token_prefix=:prefix"),{'prefix':key[:10]})
    response=client.get(f'/timelapses/{tid}/edits/'+ 'a'*32,headers={'X-API-Key':key})
    status=response.status_code;assert status==403

@pytest.mark.parametrize('case,expected',[('missing_etag',428),('malformed',400),('stale',412),('bad_id',400),('bad_speed',400)])
def test_preconditions_no_operation_written(http_edit,case,expected):
    client,engine,tid,nonce,token=http_edit
    headers={'Authorization':'Bearer '+token,'If-Match':'"'+nonce+'"'};body={'operation_id':'a'*32,'multiplier':2}
    if case=='missing_etag':headers.pop('If-Match')
    elif case=='malformed':headers['If-Match']='*'
    elif case=='stale':headers['If-Match']='"stale"'
    elif case=='bad_id':body['operation_id']='arbitrary'
    elif case=='bad_speed':body['multiplier']=True
    response=client.post(f'/timelapses/{tid}/speed',headers=headers,json=body)
    status=response.status_code;assert status==expected
    with Session(engine) as db:assert db.execute(text("SELECT count(*) FROM timelapse_publications WHERE kind IN ('trim','speed')")).scalar_one()==0

def test_foreign_org_operator_and_late_education_path_alias_refuse_without_writes(http_edit):
    client,engine,tid,nonce,owner_token=http_edit
    with engine.begin() as c:
        c.execute(text("UPDATE users SET role='operator' WHERE id=5"))
        foreign_name=c.execute(text('SELECT username FROM users WHERE id=5')).scalar_one()
    foreign_token=create_access_token({'sub':foreign_name,'role':'operator'})
    body={'operation_id':'a'*32,'multiplier':2}
    response=client.post(f'/timelapses/{tid}/speed',headers={'Authorization':'Bearer '+foreign_token,'If-Match':'"'+nonce+'"'},json=body)
    status=response.status_code;assert status==404
    with engine.begin() as c:c.execute(text("UPDATE education_upload_operations SET final_path='/owned/plain.3mf' WHERE operation_id='upload-1'"))
    response=client.post(f'/timelapses/{tid}/speed',headers={'Authorization':'Bearer '+owner_token,'If-Match':'"'+nonce+'"'},json=body)
    status=response.status_code;assert status==404
    with Session(engine) as db:assert db.execute(text("SELECT count(*) FROM timelapse_publications WHERE kind IN ('trim','speed')")).scalar_one()==0

def test_root_media_guard_contention_is_typed503_and_does_not_admit_operation(http_edit):
    from core.timelapse_media_lock import acquire_media_guard
    client,engine,tid,nonce,token=http_edit
    with Session(engine) as db:
        # Fixture route root is already bound to this owned publication root.
        from modules.printers import camera_routes
        root=camera_routes.TIMELAPSE_ROOT
    with acquire_media_guard(root,tid):
        response=client.post(f'/timelapses/{tid}/speed',headers={'Authorization':'Bearer '+token,'If-Match':'"'+nonce+'"'},json={'operation_id':'a'*32,'multiplier':2})
    status=response.status_code;assert status==503
    with acquire_media_guard(root,tid):
        response=client.head(f'/timelapses/{tid}/download',headers={'Authorization':'Bearer '+token})
    status=response.status_code;assert status==503
    with Session(engine) as db:assert db.execute(text("SELECT count(*) FROM timelapse_publications WHERE kind IN ('trim','speed')")).scalar_one()==0
