"""Audit-completeness request/transaction contracts; owned files and device substitutes."""
import csv
import io
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

pytestmark = pytest.mark.usefixtures('enterprise_license')
PEER = '198.51.100.7'


@pytest.fixture
def client(review_db, monkeypatch, tmp_path):
    from core.db import get_db
    from core.auth import create_access_token
    from core.audit_actor import register_request_session, clear_session
    from core.middleware.audit_actor import AuditActorMiddleware
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.reporting.routes import exports
    from modules.archives.routes import archives_crud, projects
    from modules.inventory.routes import spool_ops
    from modules.models_library.routes import print_files
    from modules.system import routes_backup, profile_routes, routes_settings, routes_config
    from modules.printers import camera_routes, routes_start_next
    from modules.vision.routes import detections
    from modules.jobs.routes import jobs_crud, jobs_lifecycle
    from modules.organizations import routes_oidc
    monkeypatch.delenv('API_KEY', raising=False)
    monkeypatch.setattr(registry, '_providers', registry.providers | {'EducationPolicyProvider': EducationPolicyService()})
    monkeypatch.setattr(routes_config, 'mqtt_republish', None)
    monkeypatch.setattr(jobs_crud, '_deliver_queue_entries', lambda *args: None)
    monkeypatch.setattr(routes_settings, '_branding_directory', lambda: tmp_path / 'branding')
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT', str(tmp_path))
    payload = tmp_path / 'model.stl'; payload.write_bytes(b'owned design bytes')
    backup = tmp_path / 'odin_backup_owned.db'; backup.write_bytes(b'owned backup bytes')
    monkeypatch.setattr(routes_backup, '_safe_backup_path', lambda name: backup)
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,mesh_data) VALUES(901,'owned','model.stl',:p,:m)"), {'p':str(payload), 'm':json.dumps({'vertices':[1,2,3]})})
    review_db.execute(text("INSERT INTO projects(id,name) VALUES(901,'owned project')"))
    review_db.execute(text("INSERT INTO printer_profiles(id,name,slicer,category,file_format,raw_content) VALUES(901,'owned','orca','printer','json','{}')"))
    review_db.execute(text('DELETE FROM audit_logs')); review_db.commit()
    app = FastAPI(); app.add_middleware(AuditActorMiddleware)
    for module in (exports, archives_crud, projects, spool_ops, print_files, routes_backup, profile_routes,
                   routes_settings, routes_config, camera_routes, routes_start_next, detections, jobs_crud, jobs_lifecycle, routes_oidc):
        app.include_router(module.router)
    def database(request: Request):
        with Session(review_db.get_bind()) as db:
            register_request_session(db, request)
            try: yield db
            finally: clear_session(db)
    app.dependency_overrides[get_db] = database
    c = TestClient(app, client=(PEER, 50000), raise_server_exceptions=False)
    c.headers['Authorization'] = 'Bearer ' + create_access_token({'sub':'admin','role':'admin'})
    yield c
    c.close()


def rows(db, action):
    db.rollback()
    return db.execute(text('SELECT entity_type,entity_id,details,actor_user_id,actor_username,actor_auth_kind,ip_address FROM audit_logs WHERE action=:a ORDER BY id'), {'a':action}).all()


def assert_row(db, action, entity, identifier, details, *, actor=True):
    found = rows(db, action)
    assert len(found) == 1
    row = found[0]
    assert tuple(row[:2]) == (entity, identifier)
    assert json.loads(row.details) == details
    assert tuple(row[3:]) == ((3,'admin','bearer_jwt',PEER) if actor else (None,None,None,PEER))


def refuse_audit(db, action=None):
    def fail(conn, cursor, statement, params, context, executemany):
        if statement.startswith('INSERT INTO audit_logs') and (action is None or action in params):
            raise RuntimeError('Owned audit insertion refusal')
    event.listen(db.get_bind(), 'before_cursor_execute', fail)
    return fail


DOWNLOADS = [
    ('/files/901/preview-model','print_file.downloaded','print_file',901,{'what':'model'}),
    ('/print-files/901/mesh','print_file.downloaded','print_file',901,{'what':'mesh'}),
    ('/projects/901/export','project.exported','project',901,None),
    ('/backups/odin_backup_owned.db','backup.downloaded','system',None,{'filename':'odin_backup_owned.db'}),
    ('/profiles/901/export','profile.exported','profile',901,None),
    ('/vision/training-data/export','vision.training_data_exported','vision',None,None),
    *[('/export/'+name,'export.downloaded',None,None,{'export':name,'filters':{}})
      for name in ('jobs','spools','filament-usage','models','audit-logs')],
    ('/spools/export','export.downloaded',None,None,{'export':'spools','filters':{}}),
    ('/archives/log/export','export.downloaded',None,None,{'export':'archive-print-log','filters':{}}),
]


@pytest.mark.parametrize('path,action,entity,identifier,details', DOWNLOADS)
@pytest.mark.parametrize('failure', [False, True])
def test_download_audit_before_response(client, review_db, path, action, entity, identifier, details, failure):
    listener = refuse_audit(review_db) if failure else None
    try:
        response = client.get(path)
    finally:
        if listener: event.remove(review_db.get_bind(), 'before_cursor_execute', listener)
    assert response.status_code == (503 if failure else 200), response.text
    if failure:
        assert response.json() == {'detail':'Download audit unavailable; try again later'}
        assert 'content-disposition' not in response.headers
        assert rows(review_db, action) == []
    else:
        assert_row(review_db, action, entity, identifier, details)


@pytest.mark.parametrize('failure',[False,True])
def test_timelapse_download_closes_on_audit_failure_and_playback_is_not_logged(client, review_db, monkeypatch, tmp_path, failure):
    from modules.printers import camera_routes
    from core.timelapse_fd_response import AdmittedFDResponse
    path = tmp_path/'video.mp4'; path.write_bytes(b'owned video')
    responses=[]
    def admitted(*args, **kwargs):
        # Owned descriptor substitutes media admission, not audit or response delivery.
        response=AdmittedFDResponse(os.open(path,os.O_RDONLY),size=11)
        responses.append(response);return response
    monkeypatch.setattr(camera_routes, '_read_lifetime', admitted)
    listener=refuse_audit(review_db) if failure else None
    try: response=client.get('/timelapses/901/download')
    finally:
        if listener:event.remove(review_db.get_bind(),'before_cursor_execute',listener)
    assert response.status_code==(503 if failure else 200)
    assert responses[-1].fd is None
    if not failure: assert_row(review_db,'timelapse.downloaded','timelapse',901,None)
    count=len(rows(review_db,'timelapse.downloaded'))
    assert client.get('/timelapses/901/stream').status_code==200
    assert client.get('/timelapses/901/video').status_code==200
    assert len(rows(review_db,'timelapse.downloaded'))==count


@pytest.mark.parametrize('count',[5000,5001])
def test_csv_truncation_and_private_query(client, review_db, count):
    review_db.execute(text("INSERT INTO audit_logs(action,entity_type,details) VALUES('owned','job',:details)"),
                      [{'details':json.dumps({'marker':'private@example.test'})} for _ in range(count)])
    review_db.commit()
    response=client.get('/export/audit-logs',params={'q':'private@example.test','date_from':'2020-01-01'})
    assert response.status_code==200
    assert len(list(csv.DictReader(io.StringIO(response.text))))==5000
    assert response.headers['X-ODIN-Export-Truncated']==str(count>5000).lower()
    assert response.headers['X-ODIN-Export-Row-Limit']=='5000'
    assert_row(review_db,'export.downloaded',None,None,{'export':'audit-logs','filters':{'q':True,'date_from':'2020-01-01'}})


@pytest.mark.parametrize('kind',['branding','logo','mqtt','bulk'])
@pytest.mark.parametrize('failure',[False,True])
def test_small_mutations_are_atomic(client,review_db,kind,failure,tmp_path,monkeypatch):
    from modules.organizations.branding import Branding
    from cryptography.fernet import Fernet
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    review_db.query(Branding).delete();review_db.commit()
    before_jobs=review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one();review_db.rollback()
    action={'branding':'branding.updated','logo':'branding.logo_uploaded','mqtt':'mqtt_republish_config.updated','bulk':'job.created'}[kind]
    listener=refuse_audit(review_db) if failure else None
    try:
        if kind=='branding':response=client.put('/branding',json={'app_name':'Private person'})
        elif kind=='logo':response=client.post('/branding/logo',files={'file':('private.svg',b'<svg xmlns="http://www.w3.org/2000/svg"/>','image/svg+xml')})
        elif kind=='mqtt':response=client.put('/config/mqtt-republish',json={'username':'private@example.test','password':'owned-test-password','host':'owned.invalid','private field':'private'})
        else:response=client.post('/jobs/bulk',json=[{'item_name':'private@example.test'},{'item_name':'Another person'}])
    finally:
        if listener:event.remove(review_db.get_bind(),'before_cursor_execute',listener)
    assert response.status_code==(500 if failure else 201 if kind=='bulk' else 200),response.text
    if failure:
        assert not rows(review_db,action)
        if kind in ('branding','logo'):assert review_db.query(Branding).count()==0
        if kind=='logo':assert not list((tmp_path/'branding').glob('*'))
        if kind=='mqtt':assert review_db.execute(text("SELECT count(*) FROM system_config WHERE key LIKE 'mqtt_republish_%'")).scalar_one()==0
        if kind=='bulk':assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()==before_jobs
    elif kind=='bulk':
        found=rows(review_db,action);assert len(found)==2
        assert {r.entity_id for r in found}=={r['id'] for r in response.json()}
        assert all(json.loads(r.details)=={'status':'JobStatus.PENDING'} and tuple(r[3:])==(3,'admin','bearer_jwt',PEER) for r in found)
    else:
        details={'fields':['app_name']} if kind=='branding' else {'fields':['host','password','username']} if kind=='mqtt' else None
        assert_row(review_db,action,'settings',None,details)


@pytest.mark.parametrize('endpoint',['job','start-next'])
@pytest.mark.parametrize('mode',['accepted','erased','request_failure','accepted_failure'])
def test_real_background_actor_and_atomic_claim(client,review_db,tmp_path,monkeypatch,caplog,mode,endpoint):
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    from modules.printers import dispatch,dispatch_background as background
    from core.audit_actor import erase_actor_history
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    if endpoint=='start-next':
        from modules.printers import start_next
        with engine.begin() as conn:conn.execute(text("UPDATE jobs SET status='pending' WHERE id=53"))
        monkeypatch.setattr(start_next,'next_suggestion',lambda *a,**kw:{'job_id':53,'fingerprint':'f'*64})
    from core.auth import create_access_token
    client.headers["Authorization"] = "Bearer " + create_access_token({"sub":"admin","role":"admin"})
    entered,release=threading.Event(),threading.Event()
    def adapter(*args):
        entered.set();assert release.wait(10);return True,'Owned acceptance'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',adapter)
    listener=refuse_audit(review_db,'job.dispatch_requested' if mode=='request_failure' else 'job.dispatched') if 'failure' in mode else None
    try:
        import uuid
        response=(client.post('/jobs/53/dispatch') if endpoint=='job' else client.post('/printers/59/start-next',json={
            'suggested_job_id':53,'suggestion_fingerprint':'f'*64,'request_id':str(uuid.uuid4()),
            'bed_clear_confirmed':True,'attestation':'owner_in_app'}))
        if mode=='request_failure':
            assert response.status_code==503
            assert not entered.is_set()
            with engine.connect() as c:
                assert c.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()==0
                assert c.execute(text("SELECT count(*) FROM system_config WHERE key LIKE 'dispatch_progress:%'")).scalar_one()==0
            return
        assert response.status_code==202,response.text
        assert entered.wait(5)
        assert_row(review_db,'job.dispatch_requested','job',53,{'printer_id':59})
        review_db.rollback()
        if mode=='erased':
            with Session(engine) as db:
                db.execute(text('UPDATE users SET id=id WHERE id=3'))
                erase_actor_history(db,3);db.commit()
    finally:
        release.set();assert background.drain(10);background.startup()
        if listener:event.remove(engine,'before_cursor_execute',listener)
    with engine.connect() as c:state=c.execute(text('SELECT state FROM printer_dispatch_claims WHERE job_id=53')).scalar_one()
    if mode=='accepted_failure':
        # The printer accepted the start: the claim must still say so, without the audit row.
        assert state=='accepted'
        assert rows(review_db,'job.dispatched')==[]
        assert any(record.name == 'odin.dispatch' and record.levelname == 'ERROR'
                   and 'Accepted dispatch audit row could not be recorded' in record.message
                   for record in caplog.records)
    else:
        assert state=='accepted'
        if mode=='accepted':assert_row(review_db,'job.dispatched','job',53,{'printer_id':59})
        else:assert tuple(rows(review_db,'job.dispatched')[0][3:])==(None,None,None,None)


def test_handoff_requires_live_request_binding(review_db):
    from core.audit_actor import RequestAuditScope, bind_verified_actor
    from core.audit_actor_handoff import capture_request_actor
    assert capture_request_actor(review_db) is None
    scope=RequestAuditScope(PEER);scope.register(review_db)
    bind_verified_actor(review_db,{'id':3,'username':'admin'},'session_jwt')
    transfer=capture_request_actor(review_db);scope.clear()
    assert capture_request_actor(review_db) is None
    with Session(review_db.get_bind()) as worker:
        from core.audit import log_audit
        with transfer(worker):log_audit(worker,'handoff');worker.commit()
        assert not worker.info
    row=rows(review_db,'handoff')[0]
    assert tuple(row[3:])==(3,'admin','session_jwt',PEER)
