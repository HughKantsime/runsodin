"""Actual HTTP content/audience policy; identity and owned media transport substituted."""
from types import SimpleNamespace
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text, event
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db

@pytest.fixture
def media_app(review_db,monkeypatch,tmp_path):
    from modules.printers import camera_routes
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.archives import register_providers
    from modules.organizations.services import EducationPolicyService
    from core.middleware.audit_actor import AuditActorMiddleware
    from core.audit_actor import register_request_session,clear_session
    engine=review_db.get_bind()
    def enable_fk(connection,record):connection.execute('PRAGMA foreign_keys=ON')
    event.listen(engine,'connect',enable_fk)
    raw=review_db.connection().connection.driver_connection
    raw.execute('PRAGMA foreign_keys=ON')
    assert raw.execute('PRAGMA foreign_keys').fetchone()[0]==1
    root=tmp_path/'empty-managed-root';root.mkdir(mode=0o700)
    monkeypatch.setattr(camera_routes,'TIMELAPSE_ROOT',str(root))
    class PersistedActor(dict):
        def update(self,*args,**kwargs):
            super().update(*args,**kwargs)
            review_db.execute(text('UPDATE users SET role=:role,group_id=:org WHERE id=:id'),
                              {'role':self['role'],'org':self['group_id'],'id':self['id']})
            review_db.commit()
    current={'principal':PersistedActor(id=3,username='admin',role='admin',group_id=1,_auth_kind='session_jwt')}
    def identity():pytest.fail("Fixture requests must use actual JWT dependency")
    def database(request:Request):
        with Session(review_db.get_bind(),autoflush=False) as db:
            register_request_session(db,request)
            try:yield db
            finally:clear_session(db)
    app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(camera_routes.router)
    app.dependency_overrides[get_db]=database;app.dependency_overrides[get_current_user]=identity
    monkeypatch.setattr(registry,'_providers',dict(registry._providers))
    register_providers(registry)
    original=registry.get_provider
    provider=EducationPolicyService()
    monkeypatch.setattr(registry,'get_provider',lambda name:provider if name=='EducationPolicyProvider' else original(name))
    media=tmp_path/'owned-source.mp4';media.write_bytes(b'OWNED_PRIVATE_CAPTURE')
    monkeypatch.setattr(camera_routes,'_get_timelapse_path',lambda t:media)
    class ActorClient(TestClient):
        def request(self,*args,**kwargs):
            from core.auth import create_access_token
            principal=current['principal']
            if (kwargs.get('headers') or {}).get('Authorization'):
                app.dependency_overrides.pop(get_current_user,None)
                return super().request(*args,**kwargs)
            username=review_db.execute(text('SELECT username FROM users WHERE id=:id'),{'id':principal['id']}).scalar_one()
            review_db.rollback()
            app.dependency_overrides.pop(get_current_user,None)
            headers=dict(kwargs.pop('headers',{}) or {})
            headers.setdefault('Authorization','Bearer '+create_access_token({'sub':username}))
            kwargs['headers']=headers
            return super().request(*args,**kwargs)
    return ActorClient(app),current,media,provider


def capture_plain(db,*,org=2,shared=False):
    from modules.archives import timelapse_capture
    db.execute(text("INSERT INTO printers(id,name,org_id,shared) VALUES(77,'Old private printer',:org,:shared)"),{'org':org,'shared':shared})
    db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(111,'plain.3mf','plain.3mf','/owned/plain.3mf',:org)"),{'org':org})
    db.execute(text("INSERT INTO jobs(id,item_name,printer_id,print_file_id,charged_to_org_id,status) VALUES(444,'Old print',77,111,:org,'printing')"),{'org':org});db.commit()
    tid=timelapse_capture.get_or_create_timelapse(db,77,444)
    db.execute(text("UPDATE timelapses SET status='ready' WHERE id=:id"),{'id':tid})
    db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));db.commit()
    return tid


@pytest.mark.parametrize('shared',[False,True])
def test_current_device_tenant_never_acquires_old_content_after_reassignment(media_app,review_db,shared,monkeypatch,tmp_path):
    client,current,media,_=media_app;tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=False)
    review_db.execute(text("UPDATE printers SET org_id=1,shared=:shared,name='New tenant private name' WHERE id=77"),{'shared':shared});review_db.commit()
    for path in ['', '/video','/stream','/download']:
        assert client.get(f'/timelapses/{tid}{path}').status_code==404
    result=client.get('/timelapses').json();assert result['total']==0 and result['timelapses']==[]
    current['principal'].update(id=5,group_id=2)
    detail=client.get(f'/timelapses/{tid}')
    assert detail.status_code==200
    assert detail.json()['printer_name']==('New tenant private name' if shared else None)
    assert client.get(f'/timelapses/{tid}/download').content==b'owned synthetic MP4 publication evidence'
    assert client.get('/timelapses').json()['total']==1


def capture_education(db):
    from modules.archives import timelapse_capture
    db.execute(text("UPDATE jobs SET print_file_id=11,printer_id=9,status='printing' WHERE id=13"))
    db.execute(text('UPDATE printers SET shared=0 WHERE id=9'))
    db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"));db.commit()
    tid=timelapse_capture.get_or_create_timelapse(db,9,13)
    db.execute(text("UPDATE timelapses SET status='ready' WHERE id=:id"),{'id':tid});db.commit()
    return tid


@pytest.mark.parametrize('uid,role,allowed',[(1,'viewer',True),(2,'viewer',True),(3,'admin',True),(4,'viewer',False)])
def test_education_media_uses_submission_audience_not_same_org_membership(media_app,review_db,uid,role,allowed,monkeypatch,tmp_path):
    client,current,media,_=media_app;tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=True)
    current['principal'].update(id=uid,role=role)
    for path in ['', '/video','/stream','/download']:
        response=client.get(f'/timelapses/{tid}{path}')
        assert response.status_code==(200 if allowed else 404)
        if not allowed:assert b'owned synthetic MP4 publication evidence' not in response.content
    result=client.get('/timelapses?limit=1').json()
    assert result['total']==(1 if allowed else 0)
    assert len(result['timelapses'])==(1 if allowed else 0)


def test_revoked_manager_loses_existing_media_audience_immediately(media_app,review_db,monkeypatch,tmp_path):
    client,current,_,_=media_app;tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=True)
    current['principal'].update(id=2,role='viewer')
    assert client.get(f'/timelapses/{tid}').status_code==200
    review_db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2"));review_db.commit()
    assert client.get(f'/timelapses/{tid}/stream').status_code==404
    assert client.get('/timelapses').json()['total']==0


def test_missing_education_authority_never_exposes_marked_history(media_app,review_db,monkeypatch,tmp_path):
    from core.registry import registry
    client,_,_,_=media_app;tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=True)
    monkeypatch.setattr(registry,'get_provider',lambda name:None)
    assert client.get(f'/timelapses/{tid}').status_code==404
    assert client.get('/timelapses').json()['total']==0


def test_plain_explicit_global_capture_remains_shared_after_device_change(media_app,review_db,monkeypatch,tmp_path):
    client,_,media,_=media_app;tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=False,org=None,shared=True)
    review_db.execute(text('UPDATE printers SET org_id=2,shared=0 WHERE id=77'));review_db.commit()
    assert client.get(f'/timelapses/{tid}/download').content==b'owned synthetic MP4 publication evidence'
    assert client.get(f'/timelapses/{tid}').json()['printer_name'] is None


@pytest.mark.parametrize('field,value',[('filename','other.mp4'),('printer_id',10),('print_job_id',13),('ownership_operation_id','unknown-owner')])
def test_live_tuple_or_nonce_change_cannot_reuse_original_owner(media_app,review_db,field,value,monkeypatch,tmp_path):
    client,current,_,_=media_app;tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=False,org=1)
    statements={'filename':'UPDATE timelapses SET filename=:value WHERE id=:id','printer_id':'UPDATE timelapses SET printer_id=:value WHERE id=:id','print_job_id':'UPDATE timelapses SET print_job_id=:value WHERE id=:id','ownership_operation_id':'UPDATE timelapses SET ownership_operation_id=:value WHERE id=:id'}
    review_db.execute(text(statements[field]),{'value':value,'id':tid});review_db.commit()
    assert client.get(f'/timelapses/{tid}/download').status_code==404
    assert client.get('/timelapses').json()['total']==0


def test_platform_admin_maintenance_read_does_not_grant_unknown_mutation(media_app,review_db,monkeypatch):
    from modules.printers import camera_routes
    client,current,_,_=media_app
    review_db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status) VALUES(199,9,'unknown.mp4','ready')"));review_db.commit()
    assert client.get('/timelapses/199').status_code==404
    current['principal'].update(group_id=None)
    assert client.get('/timelapses/199').status_code==200
    assert client.get('/timelapses').json()['total']==1
    monkeypatch.setattr(camera_routes,'_check_ffmpeg',lambda:pytest.fail('unknown mutation must not probe a process'))
    assert client.post('/timelapses/199/speed',headers={'If-Match':'"'+'f'*32+'"'},json={'multiplier':2,'operation_id':'a'*32}).status_code==404
    assert client.delete('/timelapses/199',headers={'If-Match':'"'+'f'*32+'"'}).status_code==404
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapses WHERE id=199')).scalar_one()==1


@pytest.mark.parametrize('stored',[2,-1,'false',None])
def test_malformed_current_device_sharing_cannot_disclose_private_name(media_app,review_db,stored):
    client,current,_,_=media_app;tid=capture_plain(review_db,org=1)
    review_db.execute(text("UPDATE printers SET org_id=2,shared=:stored,name='Other tenant private name' WHERE id=77"),{'stored':stored});review_db.commit()
    response=client.get(f'/timelapses/{tid}')
    assert response.status_code==200 and response.json()['printer_name'] is None


def test_visibility_filters_counts_before_pagination(media_app,review_db):
    from core.timelapse_ownership_schema import anchors
    client,_,_,_=media_app;tid=capture_plain(review_db,org=1)
    review_db.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status,created_at,ownership_operation_id) VALUES(999,77,444,'foreign.mp4','ready','2099-01-01','foreign-owner')"))
    review_db.execute(anchors.insert(),{'operation_id':'foreign-owner','timelapse_id':999,'printer_id':77,'scheduled_job_id':444,'filename':'foreign.mp4','owner_state':'known','owner_org_id':2,'ownership_source':'first_capture_job_file','print_file_id':111,'capture_printer_org_id':2,'capture_printer_shared':False})
    review_db.commit()
    first=client.get('/timelapses?limit=1').json()
    assert first['total']==1 and [row['id'] for row in first['timelapses']]==[tid]
    second=client.get('/timelapses?limit=1&offset=1').json()
    assert second['total']==1 and second['timelapses']==[]


def test_unavailable_speed_retains_source_owner_after_printer_reassignment(media_app,review_db,monkeypatch,tmp_path):
    from modules.printers import camera_routes
    client,current,media,_=media_app;tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=False,org=2)
    review_db.execute(text('UPDATE printers SET org_id=1,shared=0 WHERE id=77'));review_db.commit()
    current['principal'].update(id=5,group_id=2)
    from modules.archives import timelapse_edit
    from fastapi import HTTPException
    probes=[]
    def unavailable(*args,**kwargs):
        probes.append(True);raise HTTPException(503,'Owned probe deliberately unavailable')
    monkeypatch.setattr(timelapse_edit,'probe_owned_media',unavailable)
    from pathlib import Path
    row=review_db.execute(text('SELECT filename,ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).one()
    media=Path(camera_routes.TIMELAPSE_ROOT)/row.filename
    headers={'If-Match':'"'+row.ownership_operation_id+'"'}
    review_db.rollback()
    original=(media.stat().st_ino,media.read_bytes())
    response=client.post(f'/timelapses/{tid}/speed',headers=headers,json={'multiplier':2,'operation_id':'a'*32})
    assert response.status_code==503,response.text
    assert probes==[True]
    row=review_db.execute(text('SELECT a.owner_org_id,a.capture_printer_org_id,a.capture_printer_shared,a.print_file_id FROM timelapses t JOIN timelapse_ownership_anchors a ON a.operation_id=t.ownership_operation_id WHERE t.id=:id'),{'id':tid}).one()
    assert tuple(row)==(2,2,0,111)
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapses')).scalar_one()==1
    assert (media.stat().st_ino,media.read_bytes())==original
    assert client.get(f'/timelapses/{tid}').status_code==200
    current['principal'].update(id=3,group_id=1)
    assert client.post(f'/timelapses/{tid}/speed',headers=headers,json={'multiplier':2,'operation_id':'a'*32}).status_code==404


@pytest.mark.parametrize('operation',['delete','trim','speed'])
def test_foreign_owner_mutation_never_resolves_media_or_probes_process(media_app,review_db,monkeypatch,operation,managed_plain_media):
    from modules.printers import camera_routes
    client,_,_,_=media_app;tid=managed_plain_media
    monkeypatch.setattr(camera_routes,'_check_ffmpeg',lambda:pytest.fail('foreign mutation must not probe a process'))
    from core.auth import create_access_token
    from core.dependencies import get_current_user
    monkeypatch.delitem(client.app.dependency_overrides,get_current_user)
    from modules.archives import timelapse_edit,timelapse_lifetime_prepare
    for name in ('open_proven_media','probe_owned_media'):
        monkeypatch.setattr(timelapse_edit,name,lambda *a,**k:pytest.fail('Foreign actor must not acquire source or probe media'))
    for name in ('admit','work'):
        monkeypatch.setattr(timelapse_edit.EditJournal,name,lambda *a,**k:pytest.fail('Foreign actor must not admit journal or perform work'))
    nonce=review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    headers={'Authorization':'Bearer '+create_access_token({'sub':'admin'}),'If-Match':'\"'+nonce+'\"'}
    if operation=='delete':response=client.delete(f'/timelapses/{tid}',headers=headers)
    else:response=client.post(f'/timelapses/{tid}/{operation}',headers=headers,json={'operation_id':'a'*32,'multiplier':2,'start_seconds':0,'end_seconds':1})
    assert response.status_code==404,response.json().get('detail')
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==1


def test_fresh_authority_cannot_authorize_older_loaded_media_identity(media_app,review_db,monkeypatch,tmp_path):
    from modules.archives import timelapse_read_lease
    client,current,_,_=media_app
    tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=True)
    current['principal'].update(id=2,role='viewer')
    assert client.get(f'/timelapses/{tid}/download').status_code==200
    original=timelapse_read_lease.fence_authorization;admissions=[]
    def revoke_before_final_admission(db,**kwargs):
        admissions.append(True)
        if len(admissions)==2:
            with Session(review_db.get_bind()) as writer:
                writer.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2"));writer.commit()
        return original(db,**kwargs)
    monkeypatch.setattr(timelapse_read_lease,'fence_authorization',revoke_before_final_admission)
    stale=client.get(f'/timelapses/{tid}/download')
    assert len(admissions)==2
    assert stale.status_code==404 and b'owned synthetic MP4 publication evidence' not in stale.content
    # A fresh, still-authorized submitter can read the same sanctioned lifetime.
    current['principal'].update(id=1,role='viewer')
    fresh=client.get(f'/timelapses/{tid}/download')
    assert fresh.status_code==200 and fresh.content==b'owned synthetic MP4 publication evidence'


@pytest.mark.parametrize('drift',['nonce','manager_grant'])
def test_unavailable_speed_does_not_disclose_after_authority_is_lost(media_app,review_db,monkeypatch,drift,tmp_path):
    from modules.printers import camera_routes
    client,current,media,_=media_app
    if drift=='manager_grant':
        tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=True);current['principal'].update(id=2,role='operator')
        nonce=review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
        review_db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2"))
    else:
        tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=False,org=1)
        nonce=review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
        review_db.execute(text('UPDATE timelapses SET ownership_operation_id=NULL WHERE id=:id'),{'id':tid})
    review_db.commit()
    anchor_snapshot=[tuple(row) for row in review_db.execute(text('SELECT * FROM timelapse_ownership_anchors ORDER BY operation_id'))]
    from pathlib import Path
    media=Path(camera_routes.TIMELAPSE_ROOT)/review_db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    review_db.rollback()
    from modules.archives import timelapse_edit
    monkeypatch.setattr(timelapse_edit,'probe_owned_media',lambda *a,**k:pytest.fail('denied edit must not probe media'))
    original=(media.stat().st_ino,media.read_bytes())
    assert client.post(f'/timelapses/{tid}/speed',headers={'If-Match':'"'+nonce+'"'},json={'multiplier':2,'operation_id':'a'*32}).status_code==404
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT COUNT(*) FROM timelapses')).scalar_one()==1
        assert [tuple(row) for row in db.execute(text('SELECT * FROM timelapse_ownership_anchors ORDER BY operation_id'))]==anchor_snapshot
    assert (media.stat().st_ino,media.read_bytes())==original


def _managed_media(media_app,review_db,monkeypatch,tmp_path,*,education,org=2,shared=False):
    """Actual frame/publication journal; camera and codec bytes are owned synthetic."""
    import os
    from contextlib import contextmanager
    import cv2
    import numpy as np
    from modules.archives import timelapse_capture, timelapse_encode
    from modules.printers import camera_routes
    from core.sender_process_group import GroupRetirement
    from core.timelapse_media_worker import file_facts
    engine=review_db.get_bind()
    def enable_fk(connection,record):connection.execute("PRAGMA foreign_keys=ON")
    event.listen(engine,"connect",enable_fk)
    raw=review_db.connection().connection.driver_connection
    raw.execute("PRAGMA foreign_keys=ON")
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0]==1
    pid,jid=(9,13) if education else (77,444)
    tid=capture_education(review_db) if education else capture_plain(review_db,org=org,shared=shared)
    review_db.execute(text("UPDATE timelapses SET status='capturing' WHERE id=:id"),{'id':tid})
    review_db.execute(text('UPDATE printers SET timelapse_enabled=1,is_active=1 WHERE id=:pid'),{'pid':pid})
    review_db.execute(text("UPDATE jobs SET status='printing' WHERE id=:jid"),{'jid':jid})
    review_db.commit()
    nonce=review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    review_db.rollback()
    root=tmp_path/('managed-media-'+str(tid));root.mkdir(mode=0o700)
    monkeypatch.setattr(timelapse_capture,'TIMELAPSE_DIR',root)
    monkeypatch.setattr(camera_routes,'TIMELAPSE_ROOT',str(root))
    monkeypatch.setattr(timelapse_capture,'SessionLocal',lambda:Session(review_db.get_bind()))
    valid,image=cv2.imencode('.jpg',np.full((40,40,3),100,dtype=np.uint8));assert valid
    @contextmanager
    def camera(*args,**kwargs):
        yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda **kw:[image.tobytes()])
    monkeypatch.setattr(timelapse_capture.httpx,'stream',camera)
    for _ in range(3):assert timelapse_capture.capture_frame(pid,tid,nonce=nonce,job_id=jid)
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=:jid"),{'jid':jid});review_db.commit()
    class SyntheticCodecGroup:
        def __init__(self,*args,**kwargs):
            assert kwargs['defer_start'];self._media_capability=kwargs['media']
        def retire(self):
            return GroupRetirement(True,'owned synthetic codec only',channels_closed=True,media_copies_closed=True)
    def codec(group,manifest,**kwargs):
        os.write(kwargs['output_fd'],b'owned synthetic MP4 publication evidence')
        os.fsync(kwargs['output_fd']);os.fsync(kwargs['private_parent_fd'])
        return file_facts(kwargs['output_fd'])
    monkeypatch.setattr(timelapse_encode,'OwnedSenderGroup',SyntheticCodecGroup)
    monkeypatch.setattr(timelapse_encode,'complete_media_transport',codec)
    assert timelapse_capture.encode_timelapse(tid,printer_id=pid,nonce=nonce,job_id=jid)
    from core.auth import create_access_token
    username='student' if education else ('foreign' if org==2 else 'admin')
    control=media_app[0].get(f'/timelapses/{tid}/download',headers={'Authorization':'Bearer '+create_access_token({'sub':username})})
    assert control.status_code==200 and control.content==b'owned synthetic MP4 publication evidence'
    from core.dependencies import get_current_user
    media_app[0].app.dependency_overrides[get_current_user]=lambda:pytest.fail('Actual JWT required')
    return tid


@pytest.fixture
def managed_education_media(media_app,review_db,monkeypatch,tmp_path):
    return _managed_media(media_app,review_db,monkeypatch,tmp_path,education=True)


@pytest.fixture
def managed_plain_media(media_app,review_db,monkeypatch,tmp_path):
    return _managed_media(media_app,review_db,monkeypatch,tmp_path,education=False)


@pytest.mark.parametrize('kind',['trim','speed'])
@pytest.mark.parametrize('precondition',['missing','malformed','missing_operation'])
def test_edit_preconditions_refuse_before_service(media_app,review_db,managed_plain_media,monkeypatch,kind,precondition):
    from core.auth import create_access_token
    from core.dependencies import get_current_user
    from modules.archives import timelapse_edit
    client,_,_,_=media_app
    monkeypatch.delitem(client.app.dependency_overrides,get_current_user)
    monkeypatch.setattr(timelapse_edit,'execute_edit',lambda *a,**k:pytest.fail('Invalid precondition entered edit service'))
    tid=managed_plain_media
    nonce=review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    before=tuple(review_db.execute(text('SELECT filename,status,ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).one())
    headers={'Authorization':'Bearer '+create_access_token({'sub':'admin'})}
    if precondition!='missing':headers['If-Match']='invalid' if precondition=='malformed' else '"'+nonce+'"'
    body={'multiplier':2,'start_seconds':0,'end_seconds':1}
    if precondition!='missing_operation':body['operation_id']='b'*32
    response=client.post(f'/timelapses/{tid}/{kind}',headers=headers,json=body)
    assert response.status_code==(428 if precondition=='missing' else 400),response.text
    assert tuple(review_db.execute(text('SELECT filename,status,ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).one())==before


@pytest.mark.parametrize('uid,username,allowed',[(1,'student',True),(4,'outsider',False)])
def test_actual_access_jwt_resolves_student_identity_for_media_audience(media_app,review_db,managed_education_media,uid,username,allowed):
    from core.auth import create_access_token
    from core.dependencies import get_current_user
    client,_,_,_=media_app;tid=managed_education_media
    client.app.dependency_overrides.pop(get_current_user)
    with review_db.get_bind().connect() as observer:
        current_nonce=observer.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
        assert observer.execute(text('SELECT count(*) FROM timelapse_media_versions WHERE version_nonce=:n'),{'n':current_nonce}).scalar_one()==1, 'Managed fixture lacks current version binding'
    token=create_access_token(data={'sub':username,'user_id':uid,'role':'viewer','group_id':1})
    response=client.get(f'/timelapses/{tid}/download',headers={'Authorization':'Bearer '+token})
    assert response.status_code==(200 if allowed else 404)
    if allowed:assert response.content==b'owned synthetic MP4 publication evidence'
