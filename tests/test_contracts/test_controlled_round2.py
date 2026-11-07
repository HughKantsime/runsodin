"""Round-two regressions exercise production call sites and actor identity."""
import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from sqlalchemy import text, event
from tests.test_contracts.test_controlled_parts import database, actor, mark
from tests.test_contracts.test_controlled_guards import marked
from tests.test_contracts.test_dispatch_final_admission_behavior import setup, review_db


def test_inactive_member_and_wrong_audience_in_list_and_sql(marked):
    from core.controlled_parts import allowed, job_visibility
    from modules.jobs.routes.jobs_crud import list_jobs
    marked.execute(text('UPDATE users SET is_active=FALSE WHERE id=1')); marked.commit()
    assert not allowed(marked, 'job', 1, actor())
    clause, params = job_visibility(actor())
    assert marked.execute(text('SELECT id FROM jobs WHERE '+clause), params).scalars().all() == [2]
    marked.execute(text('UPDATE users SET is_active=TRUE WHERE id=1'))
    marked.execute(text('UPDATE controlled_files SET audience_id=2')); marked.commit()
    assert not allowed(marked, 'job', 1, actor())
    clause, params = job_visibility(actor())
    assert marked.execute(text('SELECT id FROM jobs WHERE '+clause), params).scalars().all() == [2]
    assert [r.id for r in list_jobs(status=None, printer_id=None, current_user=actor(), db=marked, limit=100)] == [2]


def test_get_job_callsite(marked):
    from modules.jobs.routes.jobs_crud import get_job
    with pytest.raises(HTTPException) as error:
        get_job(1, current_user=actor(2), db=marked)
    assert error.value.status_code == 403


def test_dispatch_precheck_callsite(setup, tier_license, monkeypatch, tmp_path):
    from modules.printers import dispatch
    engine, _, inputs = setup
    tier_license('enterprise')
    source = tmp_path/'fixture.gcode'; source.write_bytes(b'G1 X1')
    with engine.begin() as c:
        c.execute(text('UPDATE print_files SET stored_path=:path WHERE id=52'), {'path': str(source)})
        c.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'secure')"))
        c.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) VALUES(53,'CUI',1,'Secure',1,'fixture',CURRENT_TIMESTAMP)"))
    monkeypatch.setattr(dispatch, '_authorize_education_hardware_action', lambda *a: (False, 'precheck bypassed'))
    assert dispatch.dispatch_job(59, 53) == (False, 'controlled_part_printer_group_required')


def test_admission_recheck_callsite(setup, tier_license):
    from modules.printers.dispatch_admission import admit
    engine, _, inputs = setup
    tier_license('enterprise')
    with engine.begin() as c:
        c.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'secure')"))
        c.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) VALUES(53,'CUI',1,'Secure',1,'fixture',CURRENT_TIMESTAMP)"))
    result = admit(engine, **inputs)
    assert not result.admitted and result.reason == 'controlled_part_printer_group_required'
    with engine.connect() as c:
        assert c.execute(text('SELECT state FROM printer_dispatch_claims')).scalar() == 'preparing'


def test_backup_requires_superadmin_even_with_ack(marked, monkeypatch, tmp_path):
    from modules.system import routes_backup
    backup=tmp_path/'owned.db';backup.write_bytes(b'controlled backup')
    monkeypatch.setattr(routes_backup,'_safe_backup_path',lambda name:backup)
    with pytest.raises(HTTPException) as error:
        routes_backup._download_backup('owned.db', actor(), marked, True)
    assert error.value.detail['code'] == 'controlled_part_superadmin_required'


def test_external_started_event_is_resolved(marked):
    from core.controlled_events import event_allowed
    marked.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(50,2,'outside.gcode','completed',CURRENT_TIMESTAMP)")); marked.commit()
    message = {'type':'job_completed', 'data':{'print_job_id':50, 'job_name':'outside.gcode'}}
    assert event_allowed(actor(2), message, db=marked)


def test_mark_edits_preserve_clock_except_audience_or_shortening(marked):
    from modules.organizations.routes_controlled_parts import change_mark
    old = (datetime.now(timezone.utc)-timedelta(days=20)).isoformat()
    marked.execute(text('UPDATE controlled_files SET marked_at=:old,purge_previewed_at=:old'), {'old':old}); marked.commit()
    change_mark(marked, 'files', 1, mark(label='ITAR', printer_tag='Other', retention_days=30), actor())
    assert marked.execute(text('SELECT marked_at,purge_previewed_at FROM controlled_files')).one() == (old,old)
    change_mark(marked, 'files', 1, mark(retention_days=1), actor())
    row = marked.execute(text('SELECT marked_at,purge_previewed_at FROM controlled_files')).one()
    assert row[0] == old and row[1] is None


def test_id_redaction_no_text_or_key_oracle_and_cached_graph(marked):
    from core.controlled_redaction import redact_export
    from core.audit_actor import RequestAuditScope
    scope = RequestAuditScope('127.0.0.1')
    scope.register(marked)
    payload = {'jobs':[{'id':1,'item_name':'Part','notes':'hidden'}, {'id':2,'item_name':'Part','notes':'secret.3mf'}],
               'unrelated': {'Part': 'secret.3mf'}, 'message':'Part secret.3mf'}
    queries=[]
    def record(*args): queries.append(args[2])
    event.listen(marked.get_bind(), 'before_cursor_execute', record)
    try:
        result = redact_export(marked, actor(2), payload)
        count=len(queries)
        assert redact_export(marked, actor(2), payload) == result
        assert len(queries)==count
    finally:
        event.remove(marked.get_bind(), 'before_cursor_execute', record)
    assert result['jobs'][0]['item_name'] == '[controlled]'
    assert result['jobs'][1] == payload['jobs'][1]
    assert result['unrelated'] == payload['unrelated'] and result['message'] == payload['message']
    marked.info['unrelated_owner'] = 'preserve'
    scope.clear()
    assert marked.info == {'unrelated_owner': 'preserve'}
    from core.controlled_cache import request_cache
    assert request_cache(marked) is None


def test_audience_member_diffs_and_active_users(database, tier_license):
    from modules.organizations.routes_controlled_audiences import AudienceRequest, save
    tier_license('enterprise')
    save(database, AudienceRequest(name='Secure',user_ids=[1,2]), actor(2), 1)
    import json
    detail=database.execute(text("SELECT details FROM audit_logs WHERE action='controlled_audience.saved'")).scalar()
    detail=json.loads(detail) if isinstance(detail,str) else detail
    assert detail['before']==[1] and detail['after']==[1,2] and detail['self_added'] is True
    database.execute(text('UPDATE users SET is_active=FALSE WHERE id=1')); database.commit()
    with pytest.raises(HTTPException) as error:
        save(database, AudienceRequest(name='Secure',user_ids=[1]), actor(2), 1)
    assert error.value.status_code==422


def test_education_alias_upload_refused_even_for_a_member(marked):
    from core.controlled_education import assert_unmarked_upload_path
    from core.errors import OdinError, ErrorCode
    marked.execute(text("UPDATE print_files SET stored_path='/owned/shared.3mf' WHERE id=1")); marked.commit()
    with pytest.raises(OdinError) as error:
        assert_unmarked_upload_path(marked, '/owned/shared.3mf')
    assert error.value.code == ErrorCode.not_found
    assert error.value.detail == 'Upload authorization changed'
    assert error.value.status == 404


def test_mjpeg_frame_guard_refreshes_any_marks(database, monkeypatch, tier_license):
    from modules.printers import controlled_camera
    tier_license('enterprise')
    clock=[0]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    guard=controlled_camera.frame_guard(database.get_bind(),1)
    guard({'user_id':2})
    from modules.organizations.routes_controlled_parts import change_mark
    change_mark(database,'files',1,mark(),actor())
    database.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1")); database.commit()
    clock[0]=31
    with pytest.raises(HTTPException): guard({'user_id':2})


def test_webrtc_post_signaling_recheck(marked,monkeypatch):
    from modules.printers import camera_routes
    from tests.test_contracts.test_controlled_guards import request
    marked.execute(text('UPDATE printers SET camera_enabled=1,is_active=1 WHERE id=1'));marked.commit()
    monkeypatch.setattr(camera_routes,'audit_view',lambda *a,**k:None)
    monkeypatch.setattr(camera_routes,'get_camera_url',lambda p:'rtsp://fixture/camera')
    class Reply:
        status_code=200
        content=b'sdp'
        headers={'content-type':'application/sdp'}
        def json(self):return {'printer_1':{}}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args,**kwargs):return Reply()
        async def post(self,*args,**kwargs):
            marked.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1"));marked.commit()
            return Reply()
    monkeypatch.setattr(camera_routes.httpx,'AsyncClient',Client)
    req=request('POST','/api/cameras/1/webrtc',{'printer_id':1});req._body=b'sdp'
    with pytest.raises(HTTPException) as error:
        asyncio.run(camera_routes.camera_webrtc(1,req,marked,actor()))
    assert error.value.detail['code']=='controlled_part_use_authenticated_mjpeg'


def test_audit_list_callsite_redacts_before_response_middleware(marked):
    from modules.reporting.routes.exports import list_audit_logs
    result=list_audit_logs(limit=50,offset=0,entity_type=None,action=None,date_from=None,date_to=None,q=None,db=marked,current_user=actor(2))
    entries=[r for r in result['logs'] if r['action']=='controlled_part.changed']
    assert entries and all(r['details']=='[controlled]' for r in entries)


def test_concurrent_audience_replacements_have_serial_before_after(database,tier_license):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.orm import Session
    from modules.organizations.routes_controlled_audiences import AudienceRequest,save
    import json
    tier_license('enterprise')
    engine=database.get_bind();database.rollback()
    def replace(ids):
        with Session(engine) as db:
            return save(db,AudienceRequest(name='Secure',user_ids=ids),actor(2),1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(replace,([2],[1,2])))
    rows=database.execute(text("SELECT details FROM audit_logs WHERE action='controlled_audience.saved' ORDER BY id")).scalars().all()
    rows=[json.loads(r) if isinstance(r,str) else r for r in rows]
    assert len(rows)==2 and rows[0]['before']==[1] and rows[1]['before']==rows[0]['after']
