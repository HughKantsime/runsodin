"""Owned databases only: shelf imports, maintenance gates, RBAC and wire facts."""
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from modules.inventory.receiving import ReceiveRolls, BulkReceive
from modules.inventory.routes.receive_bulk import receive_bulk
from modules.inventory.routes.stock import receive_rolls, reorder_rollup
from modules.inventory.routes.spools import get_spool, list_spools
from modules.inventory.models import Spool, FilamentLibrary
from modules.printers.routes_maintenance_state import MaintenanceState, set_maintenance, get_maintenance
from core.printer_maintenance import read_maintenance
from core.errors import OdinError
from tests.test_contracts.test_spool_slot_prevention import graph, review_db, USER
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_dispatch_final_admission_behavior import setup

ROW = dict(brand='Bambu Lab', product_line='PLA Silk', material='PLA', color_name='South Beach',
           color_hex='#AABBCC', quantity=2, storage_location='Shelf A', note='Counted shelf rolls')


def test_receive_stores_spool_owned_color_and_library_remains_product_line(graph):
    graph.execute(text("UPDATE spools SET status='active',remaining_weight_g=250 WHERE id=199")); graph.commit()
    result = receive_rolls(ReceiveRolls(**ROW), USER, graph)
    spool = graph.get(Spool, result['spools'][0]['id'])
    assert (spool.color_name, spool.color_hex, spool.notes) == ('South Beach', 'AABBCC', 'Counted shelf rolls')
    assert spool.filament.color_hex is None and spool.filament.name == 'PLA Silk'
    detail = get_spool(spool.id, USER, graph)
    assert detail['color_name'] == 'South Beach' and detail['filament_color_hex'] == 'AABBCC'
    rows = list_spools(current_user=USER, db=graph)
    assert next(r for r in rows if r['id'] == spool.id)['color_hex'] == 'AABBCC'
    receive_rolls(ReceiveRolls(**(ROW | {'color_name': 'Rose', 'quantity': 1})), USER, graph)
    assert graph.query(FilamentLibrary).filter_by(name='PLA Silk').count() == 1
    totals = {r['color_name']: r for r in reorder_rollup(None, USER, graph)}
    assert totals['South Beach']['sealed_rolls'] == 2
    assert totals['Rose']['sealed_rolls'] == 1
    assert totals['123ABC']['total_g'] == 250


@pytest.mark.parametrize('change', [{'color_name':None}, {'color_name':' '}, {'color_hex':'blue'}, {'material':'imaginary'}, {'quantity':0}])
def test_invalid_receiving_rows(change):
    with pytest.raises(ValidationError): ReceiveRolls(**(ROW | change))


def test_bulk_atomic_reference_validation_and_replay(graph):
    before = graph.query(Spool).count(), graph.query(FilamentLibrary).count()
    valid = ReceiveRolls(**ROW)
    invalid = ReceiveRolls(filament_id=999999, color_name='White', quantity=1, storage_location='Shelf')
    with pytest.raises(OdinError):
        receive_bulk(BulkReceive(import_id='atomic', rows=[valid, invalid]), USER, graph)
    assert (graph.query(Spool).count(), graph.query(FilamentLibrary).count()) == before
    body = BulkReceive(import_id='atomic', rows=[valid, valid.model_copy(update={'color_name':'Pink'})])
    result = receive_bulk(body, USER, graph)
    assert len(result['spools']) == 4
    assert receive_bulk(body, USER, graph) == result
    assert graph.query(Spool).count() == before[0] + 4
    assert graph.query(FilamentLibrary).count() == before[1] + 1
    assert graph.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='inventory.receive'")).scalar() == 2
    with pytest.raises(HTTPException) as exc:
        receive_bulk(BulkReceive(import_id='atomic', rows=[valid]), USER, graph)
    assert exc.value.status_code == 409


def test_bulk_write_failure_rolls_back_products_spools_audit_and_import(graph, monkeypatch):
    from modules.inventory.routes import receive_bulk as module
    original = module._create_spool_record
    calls = 0
    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2: raise RuntimeError('owned failure')
        return original(*args, **kwargs)
    before = graph.query(Spool).count(), graph.query(FilamentLibrary).count()
    monkeypatch.setattr(module, '_create_spool_record', fail)
    body = BulkReceive(import_id='rollback', rows=[ReceiveRolls(**ROW)])
    with pytest.raises(RuntimeError): receive_bulk(body, USER, graph)
    assert (graph.query(Spool).count(), graph.query(FilamentLibrary).count()) == before
    assert graph.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='inventory.receive'")).scalar() == 0
    assert graph.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'inventory.import.%'")).scalar() == 0


def test_maintenance_set_clear_audited_and_cross_tenant_read_hidden(graph):
    saved = set_maintenance(MaintenanceState(state='warn', note='Part fan failing'), 9, graph, USER)
    assert saved['set_by'] == 3 and saved['set_at']
    assert get_maintenance(9, graph, {**USER, 'role':'viewer'})['note'] == 'Part fan failing'
    set_maintenance(MaintenanceState(state='ok'), 9, graph, USER)
    assert read_maintenance(graph, 9)['state'] == 'ok'
    audits = graph.execute(text("SELECT details FROM audit_logs WHERE action='printer.maintenance' ORDER BY id")).scalars().all()
    assert len(audits) == 2
    assert json.loads(audits[1])['before']['state'] == 'warn'
    graph.execute(text('UPDATE printers SET shared=0 WHERE id=9')); graph.commit()
    with pytest.raises(HTTPException): get_maintenance(9, graph, {**USER, 'role':'viewer', 'group_id':2, 'is_superadmin':False})


@pytest.mark.parametrize('state', ['warn','hold'])
def test_maintenance_admission_and_final_command(setup, review_db, state, monkeypatch):
    from modules.printers.dispatch_admission import admit
    from modules.printers import dispatch
    from modules.printers.maintenance_gate import command_context, MaintenanceHold
    engine, provider, inputs = setup
    set_maintenance(MaintenanceState(state=state, note='Replace part fan'), 59, review_db, USER)
    result = admit(engine, **inputs)
    assert result.admitted == (state == 'warn')
    calls, reports = [], []
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **k: reports.append(k))
    with command_context(engine, inputs['job']):
        if state == 'hold':
            assert 'printer_maintenance_hold' in result.reason
            with pytest.raises(MaintenanceHold): dispatch._submit_print_command(lambda: calls.append(1) or True)
            assert not calls and reports[0]['gate_refusal'].code == 'printer_maintenance_hold'
        else:
            assert dispatch._submit_print_command(lambda: calls.append(1) or True)
            assert calls == [1]


def test_start_next_hold_returns_named_refusal_before_claim(graph, monkeypatch):
    from modules.printers.start_next import start
    from modules.printers.routes_start_next import StartNext
    from modules.printers.models import Printer
    from modules.printers import maintenance_gate
    monkeypatch.setattr(maintenance_gate, 'report', lambda *a: pytest.fail('Unvalidated job must not receive an event'))
    graph.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id) VALUES(999,'Foreign job','pending',2)")); graph.commit()
    set_maintenance(MaintenanceState(state='hold', note='Fan replacement'), 9, graph, USER)
    result = start(graph, graph.get(Printer,9), USER, StartNext(suggested_job_id=999,
        suggestion_fingerprint='a'*64, request_id=uuid4(), bed_clear_confirmed=True))
    assert not result['success'] and result['code'] == 'printer_maintenance_hold'
    assert graph.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'start_next:%'")).scalar() == 0
    assert graph.execute(text('SELECT COUNT(*) FROM job_timeline_events WHERE job_id=999')).scalar() == 0


def test_new_route_rbac_and_atomic_http_validation(token_client, review_db):
    from modules.printers.routes_maintenance_state import router
    from modules.inventory.routes.receive_bulk import router as bulk_router
    client, issue = token_client
    client.app.include_router(router); client.app.include_router(bulk_router)
    headers = issue(['write:printers','read:printers','write:spools'])
    path = '/printers/9/maintenance-state'
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3")); review_db.commit()
    assert client.put(path, headers=headers, json={'state':'hold','note':'Fan'}).status_code == 403
    assert client.get(path, headers=headers).status_code == 200
    review_db.execute(text("UPDATE users SET role='admin' WHERE id=3")); review_db.commit()
    assert client.put(path, headers=issue(['read:printers']), json={'state':'hold','note':'Fan'}).status_code == 403
    assert client.put(path, headers=headers, json={'state':'hold','note':'Fan'}).status_code == 200
    body = {'import_id':'http-import', 'rows':[ROW, ROW | {'quantity':-1}]}
    assert client.post('/spools/receive/bulk',headers=headers,json=body).status_code == 422
    assert review_db.query(FilamentLibrary).filter_by(name='PLA Silk').count() == 0
    body['rows'] = [ROW]
    response = client.post('/spools/receive/bulk',headers=headers,json=body)
    assert response.status_code == 200, response.text
    assert client.post('/spools/receive/bulk',headers=headers,json=body).json() == response.json()
    review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3")); review_db.commit()
    assert client.post('/spools/receive/bulk',headers=headers,json=body).status_code == 403
    assert client.put(path, headers=headers, json={'state':'ok'}).status_code == 403
    assert client.get(path, headers=headers).json()['state'] == 'hold'


def test_concurrent_imports_write_once(graph):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    graph.commit()
    engine, barrier = graph.get_bind(), Barrier(2)
    body = BulkReceive(import_id='concurrent', rows=[ReceiveRolls(**ROW)])
    def run():
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return receive_bulk(body, USER, db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert results[0] == results[1]
    assert graph.query(Spool).filter_by(color_name='South Beach').count() == 2


def test_warn_allows_start_next_to_reach_normal_candidate_checks(graph, monkeypatch):
    from modules.printers import start_next
    from modules.printers.models import Printer
    set_maintenance(MaintenanceState(state='warn', note='Watch part fan'), 9, graph, USER)
    reached = []
    def candidate(*a, **kw):
        reached.append(True)
        return {'job_id':None,'fingerprint':None}
    monkeypatch.setattr(start_next,'next_suggestion',candidate)
    data = SimpleNamespace(request_id=uuid4(),bed_clear_confirmed=True,suggested_job_id=1,suggestion_fingerprint='a'*64)
    with pytest.raises(HTTPException) as exc: start_next.start(graph, graph.get(Printer,9), USER, data)
    assert reached == [True] and exc.value.status_code == 409
    assert 'Suggestion changed' in str(exc.value.detail)


def test_deleted_printer_does_not_leave_a_hold(graph):
    from modules.printers.models import Printer
    printer = Printer(name='Disposable maintenance printer', org_id=1)
    graph.add(printer);graph.commit()
    pid=printer.id
    set_maintenance(MaintenanceState(state='hold',note='Owned hold'),pid,graph,USER)
    graph.delete(printer);graph.commit()
    assert read_maintenance(graph,pid)['state']=='ok'


def test_hold_after_upload_is_refused_by_bambu_callback(graph):
    from modules.printers.maintenance_gate import start_gate
    gate = start_gate(graph.get_bind(),9)
    assert gate.prepare() is True
    set_maintenance(MaintenanceState(state='hold',note='Fan stopped during upload'),9,graph,USER)
    assert gate() is False
    assert gate.refusal.code == 'printer_maintenance_hold'


@pytest.mark.parametrize('spool_hex,expected', [(None, '123ABC'), ('ABCDEF', 'ABCDEF')])
def test_legacy_hex_reorder_policy_still_matches_stock(graph, spool_hex, expected):
    from modules.inventory.routes.stock import set_reorder_policy, ReorderPolicy
    graph.execute(text("UPDATE spools SET color_hex=:hex,color_name=NULL,status='active',remaining_weight_g=250 WHERE id=199"), {'hex':spool_hex})
    graph.commit()
    set_reorder_policy(ReorderPolicy(material='PLA',color='#'+expected,threshold_g=400),USER,graph)
    rows = reorder_rollup(None,USER,graph)
    assert len(rows) == 1
    assert rows[0]['color'] == expected and rows[0]['total_g'] == 250
    assert rows[0]['threshold_g'] == 400 and rows[0]['shortfall_g'] == 150


def test_existing_spool_color_can_be_named_and_cleared(graph):
    from modules.inventory.routes._helpers import SpoolUpdate
    from modules.inventory.routes.spools import update_spool
    update_spool(199,SpoolUpdate(color_name='  Nardo Gray  '),USER,graph)
    assert graph.get(Spool,199).color_name == 'Nardo Gray'
    update_spool(199,SpoolUpdate(color_name=None),USER,graph)
    assert graph.get(Spool,199).color_name is None
    for value in (' ', 'x'*101):
        with pytest.raises(ValidationError): SpoolUpdate(color_name=value)


@pytest.mark.parametrize('value,expected', [('#aabbcc','AABBCC'),('  abcdef  ','ABCDEF'),(None,None)])
def test_spool_create_normalizes_hex(value,expected):
    from modules.inventory.routes._helpers import SpoolCreate
    assert SpoolCreate(filament_id=199,color_hex=value).color_hex == expected


@pytest.mark.parametrize('value', ['red','12345','1234567','GG0000'])
def test_spool_create_rejects_invalid_hex(value):
    from modules.inventory.routes._helpers import SpoolCreate
    with pytest.raises(ValidationError): SpoolCreate(filament_id=199,color_hex=value)


def test_receive_missing_filament_retains_not_found_envelope(graph):
    from core.errors import ErrorCode
    with pytest.raises(OdinError) as exc:
        receive_rolls(ReceiveRolls(filament_id=99999,color_name='White',quantity=1,storage_location='Shelf'),USER,graph)
    assert exc.value.code == ErrorCode.not_found
    assert exc.value.status == 404
    assert exc.value.to_envelope()["error"]["code"] == "not_found"


def test_foreign_maintenance_write_refuses_before_lock(graph,monkeypatch):
    graph.execute(text('UPDATE printers SET shared=FALSE WHERE id=9'));graph.commit()
    original = graph.execute
    def execute(statement,*a,**kw):
        assert not str(statement).startswith('UPDATE printers'), 'Foreign printer must not be locked'
        return original(statement,*a,**kw)
    monkeypatch.setattr(graph,'execute',execute)
    with pytest.raises(HTTPException) as exc:
        set_maintenance(MaintenanceState(state='hold',note='Foreign'),9,graph,
                        {**USER,'group_id':2,'is_superadmin':False})
    assert exc.value.status_code == 404


def test_printer_list_and_detail_include_maintenance(graph):
    from modules.printers.routes_crud import list_printers, get_printer
    from modules.printers.schemas import PrinterResponse
    set_maintenance(MaintenanceState(state='warn',note='Fan'),9,graph,USER)
    graph.execute(text("UPDATE printers SET shared=FALSE,tags='[]',timelapse_enabled=FALSE WHERE id=9"));graph.commit()
    rows = list_printers(current_user=USER,db=graph)
    row = next(p for p in rows if p.id == 9)
    assert PrinterResponse.model_validate(row).maintenance.note == 'Fan'
    assert PrinterResponse.model_validate(get_printer(9,USER,USER,graph)).maintenance.state == 'warn'


@pytest.mark.parametrize('state', ['hold','warn'])
def test_scheduler_excludes_only_maintenance_holds(graph,state):
    from datetime import datetime, timezone
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    graph.execute(text('UPDATE printers SET is_active=FALSE WHERE id!=9'))
    graph.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority) VALUES(901,'Maintenance scheduling','pending',1,1,1,FALSE,3)"));graph.commit()
    set_maintenance(MaintenanceState(state=state,note='Fan'),9,graph,USER)
    result = Scheduler(SchedulerConfig(horizon_days=1)).run(graph,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count == (1 if state == 'warn' else 0)
    assert graph.execute(text('SELECT status FROM jobs WHERE id=901')).scalar() == ('scheduled' if state == 'warn' else 'pending')


def test_mid_upload_hold_sends_no_start_but_retains_admitted_fence(review_db,tmp_path,monkeypatch):
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    from tests.test_contracts.test_dispatch_orchestration_behavior import claim_state
    from modules.printers import dispatch
    from modules.printers.telemetry.bambu import ftp_upload, session
    notify = dispatch._notify_start_outcome
    engine,_ = configure_dispatch(review_db,tmp_path,monkeypatch,'bambu')
    monkeypatch.setattr(dispatch,'_notify_start_outcome',notify)
    calls = []
    def upload(*a,**kw):
        calls.append('upload')
        set_maintenance(MaintenanceState(state='hold',note='Fan stopped'),59,review_db,USER)
        return True
    monkeypatch.setattr(ftp_upload,'upload_file',upload)
    monkeypatch.setattr(session,'run_command',lambda *a,**k: pytest.fail('No START permitted after hold'))
    monkeypatch.setattr(dispatch,'_dispatch_bambu',dispatch._dispatch_bambu_v2)
    from modules.printers.maintenance_gate import capture_refusal
    with capture_refusal() as refusal:
        result = dispatch.dispatch_job(59,53)
    assert refusal['code'] == 'printer_maintenance_hold'
    assert not result[0] and calls == ['upload']
    assert result[1].count('printer_maintenance_hold') == 1
    assert 'no print command was sent' in result[1]
    assert claim_state(engine) == 'uncertain'


def test_truly_colorless_legacy_stock_remains_visible(graph):
    graph.execute(text("UPDATE spools SET color_name=NULL,color_hex=NULL,status='active',remaining_weight_g=250 WHERE id=199"))
    graph.execute(text('UPDATE filament_library SET color_hex=NULL WHERE id=199'));graph.commit()
    rows=reorder_rollup(None,USER,graph)
    assert len(rows)==1 and rows[0]['color']=='UNKNOWN COLOR' and rows[0]['total_g']==250


@pytest.mark.parametrize('state', ['ok', 'warn', 'hold'])
def test_maintenance_precedes_capability_and_claim(review_db, tmp_path, monkeypatch, state):
    from core import dispatch_claims
    from modules.printers import dispatch, dispatch_capability_gate, dispatch_acquisition
    from modules.printers.maintenance_gate import capture_refusal
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    notify = dispatch._notify_start_outcome
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch, 'bambu')
    monkeypatch.setattr(dispatch, '_notify_start_outcome', notify)
    set_maintenance(MaintenanceState(state=state, note='Owned merge regression'), 59, review_db, USER)
    calls = []
    if state == 'hold':
        def forbidden(*args, **kwargs):
            pytest.fail('Maintenance hold must refuse before capability, claim, or upload')
        monkeypatch.setattr(dispatch_capability_gate, 'static_refusal', forbidden)
        monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', forbidden)
        monkeypatch.setattr(dispatch_claims, 'acquire', forbidden)
        monkeypatch.setattr(dispatch, '_dispatch_bambu', forbidden)
    else:
        def adapter(*args, before_start, **kwargs):
            assert before_start.prepare() is True
            assert before_start() is True
            calls.append('START')
            return True, 'Owned START accepted'
        monkeypatch.setattr(dispatch, '_dispatch_bambu', adapter)
    with capture_refusal() as refusal:
        success, message = dispatch.dispatch_job(59, 53)
    assert success is (state != 'hold'), message
    assert calls == ([] if state == 'hold' else ['START'])
    if state == 'hold':
        assert refusal['code'] == 'printer_maintenance_hold'
        with engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one() == 0
