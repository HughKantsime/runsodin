"""A/B/D contracts: private queues, serialized ranks, strict starts and diagnostics."""
from types import SimpleNamespace as NS
import pytest
from sqlalchemy import text
from core.errors import OdinError
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_dispatch_gate_refusal_reasons import gate_case
from tests.test_contracts.test_ams_acquisition_collector import owned, ready
from modules.organizations.education_queues import list_queue, reorder_queue
from modules.organizations.routes_education import EducationQueueOrder
from modules.jobs.education_order import education_order, head_refusal

ADMIN = {'id':3,'role':'admin','group_id':1}
TEACHER = {'id':2,'role':'viewer','group_id':1}


@pytest.fixture
def queued(review_db, monkeypatch):
    db = review_db
    db.execute(text("UPDATE printers SET api_host='127.0.0.1',api_type='moonraker' WHERE id=9"))
    db.execute(text("UPDATE jobs SET status='scheduled',printer_id=9,hold=0 WHERE id=13"))
    db.execute(text("UPDATE education_submissions SET status='scheduled',approved_printer_id=9 WHERE id=14"))
    # Clone durable submission history with distinct operation, file, model and job.
    for index in (1, 2):
        params = {'n':index, 'op':f'upload-extra-{index}', 'f':20+index, 'm':30+index, 'j':40+index, 's':50+index}
        db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes) VALUES(:op,1,1,'committed',0,0)"), params)
        db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id,created_by) VALUES(:f,:op,:op,:op,1,1)"), params)
        db.execute(text("INSERT INTO models(id,name,print_file_id,org_id) VALUES(:m,:op,:f,1)"), params)
        db.execute(text("UPDATE print_files SET model_id=:m WHERE id=:f"),params)
        db.execute(text("INSERT INTO jobs(id,item_name,model_id,status,printer_id,hold,priority,charged_to_org_id,submitted_by,charged_to_user_id) VALUES(:j,:op,:m,'scheduled',9,0,5,1,1,1)"), params)
        db.execute(text("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status,lifecycle_revision,approved_printer_id) VALUES(:s,1,:op,:j,:f,:m,7,1,'scheduled',1,9)"),params)
    db.commit()
    import core.db
    monkeypatch.setattr(core.db, 'engine', db.get_bind())
    return db


def group(db, principal=ADMIN):
    return list_queue(db, principal=principal, kind='jobs')['printers'][0]


def order(db, ids, principal=ADMIN, rev=None):
    return reorder_queue(db, principal=principal, printer_id=9,
        body=EducationQueueOrder(revision=rev or group(db,principal)['revision'], job_ids=ids))


def test_audience_and_three_queues(review_db):
    assert len(list_queue(review_db,principal=TEACHER,kind='review')['items']) == 1
    assert not list_queue(review_db,principal=ADMIN,kind='jobs')['printers']
    for actor in ({'id':1}, {'id':4}):
        with pytest.raises(OdinError) as error:
            list_queue(review_db,principal=actor,kind='review')
        assert error.value.status == 403
    assert not list_queue(review_db,principal={'id':5},kind='review')['items']
    with pytest.raises(OdinError):
        list_queue(review_db,principal=TEACHER,org_id=2,kind='review')
    review_db.execute(text("UPDATE education_submissions SET status='failed'"))
    review_db.execute(text("UPDATE jobs SET status='failed'"));review_db.commit()
    assert not list_queue(review_db,principal=ADMIN,kind='review')['items']
    assert list_queue(review_db,principal=ADMIN,kind='archive')['items'][0]['can_archive']
    review_db.execute(text('UPDATE education_submissions SET archived_at=CURRENT_TIMESTAMP'));review_db.commit()
    assert not list_queue(review_db,principal=ADMIN,kind='archive',archived='unarchived')['items']
    row=list_queue(review_db,principal=ADMIN,kind='archive',archived='archived')['items'][0]
    assert row['archived'] and not row['can_archive']


def test_superadmin_explicit_org_readonly(queued):
    queued.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(88,'platform','fixture','admin',1)"));queued.commit()
    actor={'id':88,'role':'admin','group_id':None}
    with pytest.raises(Exception): list_queue(queued,principal=actor,kind='jobs')
    rows=list_queue(queued,principal=actor,org_id=1,kind='jobs')['printers'][0]['items']
    assert rows and not any(r['can_reorder'] for r in rows)
    with pytest.raises(OdinError) as error: order(queued,[42,41,13],actor,group(queued)['revision'])
    assert error.value.status == 403


def test_reorder_audit_manual_priority_and_dispatch(queued):
    from modules.printers.dispatch import _get_next_scheduled_job
    before=group(queued)['revision']
    result=order(queued,[42,41,13],TEACHER,before)
    assert result['revision'] != before
    assert [r['job_id'] for r in group(queued)['items']] == [42,41,13]
    assert _get_next_scheduled_job(9)['id'] == 42
    with queued.get_bind().connect() as conn:
        assert head_refusal(conn,{'id':13,'printer_id':9,'education_submission_id':14})
        assert head_refusal(conn,{'id':42,'printer_id':9,'education_submission_id':52}) is None
    assert queued.execute(text("SELECT COUNT(*) FROM education_audit_events WHERE action='queue.reorder'")).scalar() == 1
    with pytest.raises(OdinError) as error: order(queued,[13,41,42],rev=before)
    assert error.value.status == 409


@pytest.mark.parametrize('ids',[[13,13,42],[13,41],[13,41,999],[13,41,42,999]])
def test_invalid_full_list_atomic(queued,ids):
    before=group(queued)['revision']
    with pytest.raises(OdinError) as error: order(queued,ids,rev=before)
    assert error.value.status == 422
    assert group(queued)['revision'] == before
    assert queued.execute(text("SELECT COUNT(*) FROM education_audit_events WHERE action='queue.reorder'")).scalar() == 0


@pytest.mark.parametrize('state',['completed','printing','failed','cancelled'])
def test_terminal_and_printing_refused(queued,state):
    queued.execute(text('UPDATE jobs SET status=:state WHERE id=42'),{'state':state});queued.commit()
    with pytest.raises(OdinError) as error: order(queued,[13,41,42])
    assert error.value.status == 422


def test_wrong_org_and_generic_refused(queued):
    with pytest.raises(OdinError): order(queued,[13,41,42],{'id':5},group(queued)['revision'])
    queued.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,charged_to_org_id) VALUES(99,'generic','scheduled',9,1)"));queued.commit()
    with pytest.raises(OdinError): order(queued,[13,41,42,99])


def test_teacher_hidden_slots_preserved(queued):
    queued.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,state,revision,created_by) VALUES(8,1,'hidden','hidden','Hidden','H','active',1,3)"))
    queued.execute(text('UPDATE education_submissions SET cost_center_id=8 WHERE job_id=41'));queued.commit()
    # Initialize ranks so the hidden job is in the middle.
    order(queued,[13,41,42])
    order(queued,[42,13],TEACHER)
    assert [r['job_id'] for r in group(queued)['items']] == [42,41,13]
    assert [r['job_id'] for r in group(queued,TEACHER)['items']] == [42,13]


def test_pending_or_held_head_stalls_education_only(queued):
    from modules.printers.dispatch import _get_next_scheduled_job
    order(queued,[42,41,13])
    queued.execute(text("UPDATE jobs SET status='pending',hold=1 WHERE id=42"))
    queued.execute(text("INSERT INTO jobs(id,model_id,item_name,status,printer_id,priority,charged_to_org_id) VALUES(99,12,'generic','scheduled',9,9,1)"));queued.commit()
    assert _get_next_scheduled_job(9)['id'] == 99
    assert group(queued)['items'][0]['blocked_reason'] == 'On hold'


def test_generic_permutation_is_identical():
    rows=[NS(id=i,printer_id=9,priority=i,queue_position=None,created_at='2026') for i in range(5)]
    assert education_order(rows,set()) == rows
    rows[4].queue_position=0
    result=education_order(rows,{0,2,4})
    assert [r.id for r in result] == [4,1,0,3,2]


def test_admission_reason(gate_case,monkeypatch):
    from modules.printers import dispatch_ams_gate as gate
    checked,*_=gate_case
    monkeypatch.setattr(gate,'recheck_admitted',lambda *a,**k:NS(admitted=False,reason='Bed must be cleared'))
    assert not checked()
    assert checked.refusal.code == 'admission_changed'
    assert 'Bed must be cleared' in checked.refusal.detail


def test_refused_adapter_exception_keeps_warning(monkeypatch,caplog):
    from modules.printers import dispatch,dispatch_ams_gate
    def gate():return False
    dispatch_ams_gate.record_refusal(gate,'target_lock_lost','Printer target lock is no longer held')
    monkeypatch.setattr(dispatch,'_ws',lambda *a:None)
    monkeypatch.setattr(dispatch,'_notify_start_outcome',lambda *a,**k:None)
    monkeypatch.setattr(dispatch,'_dispatch_bambu',lambda *a,**k:(_ for _ in ()).throw(RuntimeError()))
    result=dispatch._run_admitted_dispatch({'id':1,'printer_id':9,'item_name':'test'},
        {'api_type':'bambu','ip':'localhost'},'','','',None,None,before_start=gate)
    assert not result[0]
    assert 'Adapter outcome requires reconciliation' in caplog.text


@pytest.mark.parametrize("error", [0, 42])
def test_partial_diagnostics_preserved(owned, error):
    collector,clock=owned
    ready(collector)
    initial={"ams":{"ams":[{"id":0,"tray":[{"id":0,"tray_type":"PETG"}]}]}}
    initial.update(gcode_state='IDLE',print_error=error,hms=[{'attr':1,'code':2}])
    collector.observe(incarnation=collector.incarnation,topic='device/synthetic/report',retained=False,packet=initial)
    collector.observe(incarnation=collector.incarnation,topic='device/synthetic/report',retained=False,packet={'gcode_state':'RUNNING'})
    snapshot=collector.snapshot(configuration_key='a'*64,owner_org_id=1)
    assert snapshot.print_error == error and snapshot.active_hms_codes == ('00000001_00000002',)
    assert not snapshot.idle_without_error
    collector.observe(incarnation=collector.incarnation,topic='device/synthetic/report',retained=False,
        packet={"ams":{"ams":[{"id":0,"tray":[{"id":0,"tray_type":"PETG"}]}]}})
    snapshot=collector.snapshot(configuration_key='a'*64,owner_org_id=1)
    assert snapshot.print_error == error and snapshot.active_hms_codes == ('00000001_00000002',)
    assert not snapshot.idle_without_error


def test_claim_changes_revision_and_excludes_inflight(queued):
    before = group(queued)['revision']
    queued.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,token,owner_org_id,state) VALUES(9,41,:key,:config,:hash,:token,1,'preparing')"),
        {'key':'a'*64,'config':'b'*64,'hash':'c'*64,'token':'d'*32});queued.commit()
    current = group(queued)
    assert current['revision'] != before
    committed = next(r for r in current['items'] if r['job_id'] == 41)
    assert committed['section'] == 'committed' and not committed['can_reorder']
    with pytest.raises(OdinError) as error: order(queued,[13,41,42],rev=before)
    assert error.value.status == 409
    with pytest.raises(OdinError) as error: order(queued,[13,41,42])
    assert error.value.status == 422
    order(queued,[42,13])
    assert queued.execute(text('SELECT queue_position FROM jobs WHERE id=41')).scalar() is None


def test_reorder_serializes_with_dispatch_target(queued):
    from core.auth import SECRET_KEY
    from core.dispatch_identity import build_identity
    from core.dispatch_target_lock import acquire_target_lock
    from modules.printers.dispatch import _get_printer_info
    identity = build_identity(_get_printer_info(9), SECRET_KEY.encode())
    held = acquire_target_lock(queued.get_bind(), identity.target_key)
    assert held.status == 'acquired'
    try:
        with pytest.raises(OdinError) as error: order(queued,[42,41,13])
        assert error.value.status == 409
    finally:
        assert held.handle.close().released
    order(queued,[42,41,13])


def test_explicit_dispatch_rechecks_head_under_lock(queued, monkeypatch):
    from modules.printers import dispatch, dispatch_orchestration as orchestration
    from core import dispatch_claims
    order(queued,[42,41,13])
    job={'id':13,'printer_id':9,'status':'scheduled','stored_path':'/inert',
         'education_submission_id':14}
    monkeypatch.setattr(dispatch,'_load_job',lambda *a:job)
    monkeypatch.setattr(dispatch,'_authorize_education_hardware_action',lambda *a:(True,'approved'))
    monkeypatch.setattr(orchestration,'ownership_refusal',lambda *a:None)
    monkeypatch.setattr(dispatch_claims,'retire_terminal_locked',lambda *a,**k:'clear')
    monkeypatch.setattr(orchestration,'unresolved_target',lambda *a:False)
    monkeypatch.setattr(orchestration,'_dispatch_claimed',lambda *a:pytest.fail('reserved a non-head'))
    result=orchestration.dispatch_prepared(job,dispatch._get_printer_info(9),'/inert','inert.3mf','a'*64,(True,'approved'))
    assert not result[0] and 'head must start first' in result[1]


def test_auto_education_refusal_does_not_starve_generic(monkeypatch):
    from modules.printers import dispatch
    monkeypatch.setenv('AUTO_DISPATCH','true')
    calls=[]
    monkeypatch.setattr(dispatch,'_get_next_scheduled_job',lambda pid,**kw:
        {'id':99,'item_name':'generic'} if kw.get('generic_only') else {'id':42,'item_name':'head','education_submission_id':52})
    monkeypatch.setattr(dispatch,'dispatch_job',lambda pid,jid:(calls.append(jid) or False,'blocked'))
    dispatch.attempt_dispatch(9)
    assert calls == [42,99]


def test_live_grant_revocation_refuses_cached_teacher(queued):
    rev=group(queued,TEACHER)['revision']
    queued.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2"));queued.commit()
    with pytest.raises(OdinError) as error: order(queued,[42,41,13],TEACHER,rev)
    assert error.value.status == 403


def test_http_queue_contract_and_conflict(queued, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.organizations import education_access
    from modules.organizations.routes_education import router
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_db] = lambda: queued
    app.dependency_overrides[get_current_user] = lambda: {**TEACHER,'is_active':True,'_auth_kind':'session_jwt'}
    monkeypatch.setattr(education_access, 'require_education_license', lambda: None)
    @app.exception_handler(OdinError)
    async def failure(request, exc):
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())
    with TestClient(app) as client:
        response = client.get('/api/v1/education/queues/jobs')
        assert response.status_code == 200
        revision = response.json()['printers'][0]['revision']
        response = client.patch('/api/v1/education/queues/jobs/9/order',json={'revision':revision,'job_ids':[42,41,13]})
        assert response.status_code == 200
        response = client.patch('/api/v1/education/queues/jobs/9/order',json={'revision':revision,'job_ids':[13,41,42]})
        assert response.status_code == 409 and response.json()['error']['code'] == 'revision_conflict'
        assert client.get('/api/v1/education/queues/archive?archived=invalid').status_code == 422


def test_scheduler_traverses_manual_order(queued,monkeypatch):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    from modules.organizations.services import EducationPolicyService
    from core.registry import registry
    from datetime import datetime,timezone
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    queued.execute(text("UPDATE printers SET api_type='bambu' WHERE id=9"))
    queued.execute(text("UPDATE jobs SET status='pending',quantity=1,duration_hours=0.1,priority=3 WHERE id IN (13,41,42)"))
    queued.execute(text("UPDATE education_submissions SET status='pending'"))
    # All jobs have the same approved compatible file facts for this traversal check.
    facts=queued.execute(text('SELECT compatibility_facts_json FROM print_files WHERE id=11')).scalar()
    queued.execute(text('UPDATE print_files SET compatibility_facts_json=:facts'),{'facts':facts})
    queued.commit()
    # Record ranks directly to isolate scheduler traversal from transport identity.
    queued.execute(text('UPDATE jobs SET queue_position=CASE id WHEN 42 THEN 0 WHEN 41 THEN 1 ELSE 2 END'))
    queued.commit()
    result=Scheduler(SchedulerConfig(horizon_days=1)).run(queued,start_date=datetime(2026,10,23,12,tzinfo=timezone.utc))
    assert result.scheduled_count == 3
    assert [a.job_id for a in result.assignments] == [42,41,13]


def test_platform_slice_inspection_requires_explicit_school(review_db):
    from fastapi import HTTPException
    from modules.organizations.education_slice_info import slice_info
    review_db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
    review_db.commit()
    platform={'id':88,'role':'admin','group_id':None}
    with pytest.raises(HTTPException) as error:
        slice_info(14,9,platform,review_db)
    assert error.value.status_code == 400
    # Fixture has deliberately no physical payload. Reaching metadata's 409
    # proves read authorization, while the mutation APIs still refuse platform.
    with pytest.raises(HTTPException) as error:
        slice_info(14,9,platform,review_db,org_id=1)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        slice_info(14,9,TEACHER,review_db,org_id=2)
    assert error.value.status_code == 404
