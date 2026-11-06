"""Round-three regressions at resource, rendering and transport call sites."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from tests.test_contracts.test_controlled_parts import database, actor, mark
from tests.test_contracts.test_controlled_guards import marked


def finished_print(db):
    db.execute(text("UPDATE jobs SET status='completed',printer_id=1 WHERE id=1"))
    db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) VALUES(50,1,1,'secret.3mf','completed',CURRENT_TIMESTAMP)"))
    db.commit()


@pytest.mark.parametrize('surface', ['websocket', 'status', 'crud'])
def test_finished_printer_names_remain_controlled(marked, monkeypatch, surface):
    from modules.printers import routes_status, routes_crud
    from core.controlled_events import audience_messages
    import core.db
    finished_print(marked)
    payload = dict(printer_id=1, gcode_file='secret.3mf', subtask_name='secret.3mf', job_name='secret.3mf')
    monkeypatch.setattr(core.db, 'engine', marked.get_bind())
    monkeypatch.setattr(routes_status, '_fetch_printer_live_status', lambda *a: payload)
    marked.execute(text("UPDATE printers SET api_host='fixture',api_key='fixture' WHERE id=1")); marked.commit()
    def read(who):
        if surface == 'websocket':
            return audience_messages([who], {'type':'printer_telemetry','data':payload})[0]['data']
        if surface == 'status':
            return routes_status.get_printer_live_status(1, db=marked, current_user=who)
        return routes_crud.get_all_printers_live_status_early(db=marked, current_user=who)[0]
    assert all(read(actor(2))[key] == '[controlled]' for key in ('gcode_file','subtask_name','job_name'))
    assert read(actor()) == payload
    # New physical work, including an outside-ODIN print, replaces the stale name.
    marked.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(51,1,'public.gcode','running',CURRENT_TIMESTAMP)")); marked.commit()
    assert read(actor(2)) == payload


def test_gdpr_other_user_resource_collections(marked):
    from modules.organizations.routes_sessions import export_user_data
    marked.execute(text('UPDATE jobs SET submitted_by=1,charged_to_user_id=1 WHERE id=1'))
    marked.execute(text('UPDATE print_files SET created_by=1 WHERE id=1'))
    marked.execute(text("INSERT INTO print_archives(id,job_id,user_id,print_name,status) VALUES(70,1,1,'secret archive','completed')")); marked.commit()
    result = asyncio.run(export_user_data(1, actor(2), marked))
    for collection, field in [('jobs_submitted','item_name'),('jobs_charged','item_name'),('print_archives','print_name'),('uploaded_print_files','filename')]:
        assert result[collection][0][field] == '[controlled]', collection
    own = asyncio.run(export_user_data(1, actor(), marked))
    assert own['print_archives'][0]['print_name'] == 'secret archive'


def test_move_conflict_hides_controlled_name(marked, monkeypatch):
    from modules.organizations.education_policy import assert_generic_job_mutation_allowed
    monkeypatch.setattr('modules.jobs.routes.jobs_lifecycle.assert_generic_job_mutation_allowed', assert_generic_job_mutation_allowed)
    from modules.jobs.routes.jobs_lifecycle import move_job, MoveJobRequest
    start = datetime.now(timezone.utc).replace(tzinfo=None)
    marked.execute(text("UPDATE jobs SET status='scheduled',printer_id=1,scheduled_start=:s,scheduled_end=:e WHERE id=1"), {'s':start, 'e':start+timedelta(hours=2)})
    marked.execute(text('UPDATE jobs SET duration_hours=1,quantity=1 WHERE id=2')); marked.commit()
    with pytest.raises(HTTPException) as exc:
        move_job(2, MoveJobRequest(printer_id=1,scheduled_start=start), actor(2), marked)
    assert exc.value.status_code == 400
    assert exc.value.detail == 'Time slot conflicts with another job'


def seed_order(db):
    db.execute(text("INSERT INTO products(id,name,sku) VALUES(1,'Secret product','Secret SKU')"))
    db.execute(text('INSERT INTO product_components(id,product_id,model_id,quantity_needed) VALUES(1,1,1,1)'))
    db.execute(text("INSERT INTO orders(id,order_number,status) VALUES(1,'Public order','pending')"))
    db.execute(text('INSERT INTO order_items(id,order_id,product_id,quantity,unit_price,fulfilled_quantity) VALUES(1,1,1,1,12,0)')); db.commit()


def test_invoice_redacts_before_pdf_rendering(marked):
    from modules.orders.routes.orders_crud import get_order_invoice
    from pypdf import PdfReader
    seed_order(marked)
    result = get_order_invoice(1, actor(2), marked)
    content = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(result.body)).pages)
    assert '[controlled]' in content and 'Secret product' not in content and 'Secret SKU' not in content
    result = get_order_invoice(1, actor(), marked)
    content = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(result.body)).pages)
    assert 'Secret product' in content and 'Secret SKU' in content


def test_alert_read_redacts_stored_copy_after_membership_revocation(marked):
    from modules.notifications.routes.alerts import list_alerts
    marked.execute(text("INSERT INTO alerts(user_id,job_id,alert_type,severity,title,message) VALUES(2,1,'print_complete','info','Secret title','Secret message')")); marked.commit()
    result = asyncio.run(list_alerts(current_user=actor(2), db=marked))
    assert len(result) == 1
    assert result[0].title == result[0].message == result[0].job_name == '[controlled]'
    assert marked.execute(text('SELECT title FROM alerts')).scalar() == 'Secret title'


def test_deletion_scrubs_prior_audit_in_list_and_csv(marked, monkeypatch):
    from modules.organizations.education_policy import assert_generic_job_mutation_allowed
    monkeypatch.setattr('modules.jobs.routes.jobs_crud.assert_generic_job_mutation_allowed', assert_generic_job_mutation_allowed)
    from core.audit import log_audit
    from modules.jobs.routes.jobs_crud import delete_job
    from modules.reporting.routes.exports import list_audit_logs, export_audit_logs_csv
    log_audit(marked, 'job.updated', 'job', 1, {'item_name':'Secret old name'}); marked.commit()
    delete_job(1, actor(), marked)
    result = list_audit_logs(limit=50,offset=0,entity_type=None,action=None,date_from=None,date_to=None,q=None,db=marked,current_user=actor(2))
    rows = [r for r in result['logs'] if r['action'] == 'job.updated']
    assert rows and rows[0]['details'] == '[controlled]'
    response = export_audit_logs_csv(entity_type=None,action=None,date_from=None,date_to=None,q=None,db=marked,current_user=actor(2))
    async def body():
        return ''.join([part async for part in response.body_iterator])
    assert 'Secret old name' not in asyncio.run(body())


def test_mqtt_other_process_mark_refresh(database, monkeypatch, tier_license):
    from core import controlled_mqtt
    from modules.notifications import mqtt_republish
    engine = database.get_bind()
    clock = [0]
    monkeypatch.setattr(controlled_mqtt.time, 'monotonic', lambda: clock[0])
    controlled_mqtt.invalidate()
    payload = {'job_id':1, 'job_name':'Secret'}
    tier_license('enterprise')
    from types import SimpleNamespace
    sent = []
    monkeypatch.setattr(mqtt_republish, 'engine', engine)
    monkeypatch.setattr(mqtt_republish, '_get_config', lambda: {'topic_prefix':'fixture'})
    monkeypatch.setattr(mqtt_republish, '_get_client', lambda: SimpleNamespace(publish=lambda topic, message, **kw: sent.append(json.loads(message))))
    mqtt_republish._publish('job', payload)
    assert sent[-1] == payload
    # Commit directly: no local invalidate(), as with a separate API worker.
    database.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) VALUES(1,'CUI',1,'Secure',7,'fixture',CURRENT_TIMESTAMP)")); database.commit()
    clock[0] = 1.1
    mqtt_republish._publish('job', payload)
    assert sent[-1].get('title') == 'Controlled work'


@pytest.mark.parametrize('status,kind,method', [(200,'text/html','GET'),(204,None,'GET'),(200,'application/json','HEAD'),(302,'application/json','GET'),(403,'application/json','GET')])
def test_middleware_skips_credentials_for_non_json_responses(monkeypatch,status,kind,method):
    from core.middleware.controlled_response import controlled_response
    from starlette.requests import Request
    from starlette.responses import Response
    import core.dependencies
    calls = []
    async def resolve(*a):
        calls.append(True)
        return None
    monkeypatch.setattr(core.dependencies, '_resolve_current_user', resolve)
    response = Response(b'', status_code=status, media_type=kind)
    async def next_response(request): return response
    request = Request({'type':'http','method':method,'path':'/','headers':[]})
    assert asyncio.run(controlled_response(request,next_response)) is response
    assert calls == []


def test_audience_round_trip_cannot_extend_retention(marked):
    from modules.organizations.routes_controlled_parts import change_mark
    old = (datetime.now(timezone.utc)-timedelta(days=20)).isoformat()
    marked.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(2,1)'))
    marked.execute(text('UPDATE controlled_files SET marked_at=:old'), {'old':old}); marked.commit()
    change_mark(marked,'files',1,mark(audience_id=2),actor())
    change_mark(marked,'files',1,mark(audience_id=1),actor())
    assert marked.execute(text('SELECT marked_at FROM controlled_files')).scalar() == old


@pytest.mark.parametrize('stream', [False, True])
def test_admin_log_callsite_requires_acknowledgement(marked, tmp_path, monkeypatch, stream):
    from modules.system import routes_admin
    from starlette.requests import Request
    path = tmp_path/'application.log'; path.write_text('Secret printed name\n')
    monkeypatch.setattr(routes_admin, '_LOG_FILES', {'backend':str(path)})
    req = Request({'type':'http','method':'GET','path':'/admin/logs','headers':[]})
    with pytest.raises(HTTPException) as exc:
        if stream:
            asyncio.run(routes_admin.stream_logs(req,source='backend',level=None,search=None,user=actor(2),db=marked,token=__import__('core.controlled_logs',fromlist=['issue_stream_token']).issue_stream_token(actor(2),False)))
        else:
            routes_admin.get_logs(source='backend',lines=20,level=None,search=None,cursor=None,user=actor(2),db=marked)
    assert exc.value.status_code == 403 and exc.value.detail['code'] == 'controlled_part_acknowledgement_required'
    if not stream:
        result = routes_admin.get_logs(source='backend',lines=20,level=None,search=None,cursor=None,user=actor(2),db=marked,acknowledge_controlled=True)
        assert result['lines'] == ['Secret printed name']
        assert marked.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.privileged_export'")).scalar() == 1


def test_log_stream_rechecks_marks_after_connect(database, tmp_path, monkeypatch):
    from modules.system import routes_admin
    path = tmp_path/'application.log'; path.write_text('')
    monkeypatch.setattr(routes_admin, '_LOG_FILES', {'backend':str(path)})
    class Request:
        async def is_disconnected(self):
            database.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) VALUES(1,'CUI',1,'Secure',7,'fixture',CURRENT_TIMESTAMP)")); database.commit()
            with path.open('a') as f: f.write('Secret printed name\n')
            return False
    async def read():
        response = await routes_admin.stream_logs(Request(),source='backend',level=None,search=None,user=actor(2),db=database,token=__import__('core.controlled_logs',fromlist=['issue_stream_token']).issue_stream_token(actor(2),False))
        return await anext(response.body_iterator)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read())
    assert exc.value.status_code == 403
