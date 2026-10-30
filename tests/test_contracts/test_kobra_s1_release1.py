"""Release-1 offline contracts. Device and MQTT boundaries are always substituted."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine, text
from modules.printers.kobra_s1 import parse_ace, parse_header, build_payload, S1Refusal, verify_mapping
from modules.printers.adapters.moonraker import MoonrakerPrinter
from modules.printers.dispatch_support import is_kobra_s1, dispatch_unsupported_reason, automatic_dispatch_unsupported_reason

FIXTURES = Path(__file__).resolve().parents[2] / 'backend/tests/fixtures/kobra_s1'
HUB = json.loads((FIXTURES / 'filament_hub.json').read_text())['result']['status']['filament_hub']
# The proven Bambu Studio start header (benchy/torture files, Oct 29).
S1_HEADER = '; HEADER_BLOCK_START\n; BambuStudio 02.08.02.61\n; HEADER_BLOCK_END\n; curr_bed_type = Textured PEI Plate\n; textured_plate_temp_initial_layer = 55\n'
FILAMENTS = parse_header('; filament_colour = #00AE42;#FF00FF\n; filament_type = PLA;PLA')


def test_ace_non_rfid_humidity_loaded_and_unknown_material():
    ace = parse_ace(HUB)
    assert all(s['usable'] for s in ace['slots'])
    assert ace['humidity'] is None and ace['current_filament'] == '' and ace['auto_refill'] == 1
    assert ace['slots'][1]['color_hex'] == '6558B1'
    hub = copy.deepcopy(HUB)
    hub['current_filament'] = '0-1'
    hub['filament_hubs'][0]['slots'][1]['type'] = 'Future polymer'
    ace = parse_ace(hub)
    assert ace['current_filament'] == '0-1' and ace['slots'][1]['type'] == 'Future polymer'
    with pytest.raises(S1Refusal, match='s1_material_mismatch'):
        verify_mapping(FILAMENTS[:1], ace['slots'], [1])
    assert not any(s['usable'] for s in parse_ace(None)['slots'])


@pytest.mark.parametrize('mapping', [[1], [1, 3]])
def test_payload_equals_proven_reference(mapping):
    spec = importlib.util.spec_from_file_location('s1_reference', FIXTURES / 'reference_payload.py')
    reference = importlib.util.module_from_spec(spec); spec.loader.exec_module(reference)
    file = dict(path='folder/fixture.gcode', size=912, md5='a'*32, filaments=FILAMENTS[:len(mapping)])
    slots = parse_ace(HUB)['slots']
    expected = reference.build_payload(file, slots, mapping,
        dict(auto_leveling=False, flow_calibration=False, vibration_compensation=False), 123, 'fixture')
    actual = build_payload(file, slots, mapping, 123, 'fixture')
    assert actual == expected
    assert [m['ams_index'] for m in actual['data']['ams_settings']['ams_box_mapping']] == mapping


@pytest.mark.parametrize('mapping,code', [(None,'ams_mapping_required'), ([], 's1_mapping_incomplete'),
    ([True], 's1_mapping_incomplete'), ([4], 's1_mapping_incomplete'), ([-1], 's1_mapping_incomplete'), ('bad','ams_mapping_required')])
def test_mapping_refusals(mapping, code):
    with pytest.raises(S1Refusal, match=code):
        verify_mapping(FILAMENTS[:1], parse_ace(HUB)['slots'], mapping)


@pytest.mark.parametrize('change,code', [({'usable':False},'s1_slot_unusable'),
    ({'color':None},'s1_slot_color_unknown'), ({'type':'PETG'},'s1_material_mismatch')])
def test_slot_refusals(change, code):
    slots = parse_ace(HUB)['slots']; slots[1].update(change)
    with pytest.raises(S1Refusal, match=code):
        verify_mapping(FILAMENTS[:1], slots, [1])


@pytest.mark.parametrize('field', ['model','machine_type'])
def test_identity_never_host_and_plain_start_blocked(field, monkeypatch):
    identity = dict(api_type='moonraker', **{field:'Anycubic Kobra S1'})
    assert is_kobra_s1(identity) and dispatch_unsupported_reason(identity) is None
    assert automatic_dispatch_unsupported_reason(identity).startswith('s1_explicit_dispatch_required')
    assert not is_kobra_s1(dict(api_type='moonraker', host='Kobra S1'))
    p = MoonrakerPrinter('192.0.2.1', **{field:'Kobra S1'})
    monkeypatch.setattr(p, '_post', lambda *a: pytest.fail('Plain start forbidden'))
    assert p.start_print('x.gcode') is False
    from modules.printers.dispatch import _dispatch_moonraker
    assert _dispatch_moonraker(1,'unused','x.gcode',identity)[0] is False


def test_adapter_queries_hidden_object_and_preserves_happy_hare(monkeypatch):
    body = json.loads((FIXTURES / 'filament_hub.json').read_text())
    seen=[]
    p=MoonrakerPrinter('192.0.2.1',model='Kobra S1')
    monkeypatch.setattr(p,'_get',lambda path: seen.append(path) or body)
    s=p.get_status()
    assert 'filament_hub' in seen[0] and s.ace_status['humidity'] is None
    assert [s.gate for s in s.filament_slots]==[0,1,2,3]
    p=MoonrakerPrinter('192.0.2.1',model='Voron')
    monkeypatch.setattr(p,'_get',lambda path: seen.append(path) or body)
    assert len(p.get_status().filament_slots)==4
    assert 'filament_hub' not in seen[-1]


class _DeviceResponse(__import__('io').BytesIO):
    def __init__(self, body, declared='auto'):
        super().__init__(body)
        self.headers = {} if declared is None else {'Content-Length': str(len(body) if declared == 'auto' else declared)}


def test_upload_409_is_busy_and_device_identity_hashes_readback(tmp_path, monkeypatch):
    from core import webhook_utils
    p=MoonrakerPrinter('192.0.2.1',model='Kobra S1')
    local=tmp_path/'print.gcode';local.write_bytes(S1_HEADER.encode() + b'; filament_colour = #00AE42\n; filament_type = PLA\nG28\n')
    monkeypatch.setattr(webhook_utils,'device_request',lambda *a,**k:SimpleNamespace(status_code=409))
    assert p.upload_file(str(local)) is False and p.last_upload_busy
    device=b'; Rinkhals added this line\n'+local.read_bytes()
    import io
    from modules.printers.adapters import moonraker
    reads=[]
    def copy(*a,**k):
        reads.append(True)
        return _DeviceResponse(device)
    monkeypatch.setattr(moonraker,'urlopen',copy)
    facts=p.device_file_identity('folder/print.gcode')
    assert len(reads)==2
    assert facts['size']==len(device) and facts['md5']==hashlib.md5(device).hexdigest()
    assert facts['md5']!=hashlib.md5(local.read_bytes()).hexdigest()


@pytest.fixture
def inventory(tmp_path):
    from core.schema import bootstrap_database
    engine=create_engine('sqlite:///'+str(tmp_path/'inventory.db'))
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO printers(id,name,api_type,model) VALUES(1,'Fixture','moonraker','Kobra S1')"))
        c.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(1,'Fixture','PLA')"))
        c.execute(text('INSERT INTO spools(id,filament_id) VALUES(1,1)'))
        c.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,assigned_spool_id,filament_type,do_not_feed,do_not_feed_note) VALUES(1,1,1,1,'PLA',true,'Feed fault')"))
    yield engine
    engine.dispose()


def test_ace_sync_keeps_assignments_flags_and_unknown_material(inventory):
    from modules.printers.monitors.ams_inventory import sync_ace_inventory
    ace=parse_ace(HUB);ace['slots'][0]['type']='Future polymer'
    with inventory.begin() as c:
        sync_ace_inventory(c,1,ace)
        assert c.execute(text('SELECT assigned_spool_id,do_not_feed,do_not_feed_note,filament_type,color FROM filament_slots WHERE id=1')).one()==(1,1,'Feed fault','Unknown','Future polymer')
        stored=json.loads(c.execute(text('SELECT ace_status FROM printers WHERE id=1')).scalar_one())
        assert stored['humidity'] is None and stored['slots'][0]['type']=='Future polymer'
        assert c.execute(text('SELECT slot_number FROM filament_slots ORDER BY slot_number')).scalars().all()==[1,2,3,4]


def test_flags_all_protocols_and_admin_audit(inventory):
    from sqlalchemy.orm import Session
    from modules.printers.models import FilamentSlot
    from modules.printers.slot_feed_policy import flagged_mapping_reason,policy_change
    from fastapi import HTTPException
    with inventory.connect() as c:
        assert flagged_mapping_reason(c,1,[0])=='slot_do_not_feed: Slot 1: Feed fault'
        assert flagged_mapping_reason(c,1,[1]) is None
    with Session(inventory) as db:
        slot=db.get(FilamentSlot,1)
        for role in ['viewer','operator']:
            with pytest.raises(HTTPException) as e:
                policy_change(db,slot,{'do_not_feed':False},{'role':role})
            assert e.value.status_code==403
        details=policy_change(db,slot,{'do_not_feed':False,'do_not_feed_note':'Feed passed'},{'role':'admin'})
        assert details['before']=={'do_not_feed':True,'note':'Feed fault'}
        assert details['after']=={'do_not_feed':False,'note':'Feed passed'} and details['note']=='Feed passed'
        assert policy_change(db,slot,{'do_not_feed':True,'do_not_feed_note':'Feed fault'},{'role':'admin'}) is None


@pytest.mark.parametrize('outcome', ['correlated','no_report','disconnect_after_publish','publish_rejected','publish_timeout','handshake_failed',
                                     'refused','refused_then_disconnect','report_without_code','report_with_unusable_code','code_200','other_model'])
def test_one_shot_mqtt_no_retry(monkeypatch, outcome):
    from modules.printers.adapters import anycubic_lan as module
    from core import webhook_utils
    import paho.mqtt.client as mqtt
    calls=[]
    class Client:
        def __init__(self,*a,**kw):
            assert kw['reconnect_on_failure'] is False
        def username_pw_set(self,*a):pass
        def tls_set_context(self,*a):pass
        def connect(self,*a,**kw):
            self.on_connect(self,None,None,SimpleNamespace(is_failure=False))
        def subscribe(self,*a,**kw):
            self.on_subscribe(self,None,1,[SimpleNamespace(is_failure=False)])
        def publish(self,*a,**kw):
            calls.append((a,kw))
            body=json.loads((FIXTURES/'print_start_report.json').read_text())
            if outcome in ('correlated','refused','refused_then_disconnect','report_without_code','report_with_unusable_code','code_200'):body['msgid']='fixture'
            if outcome in ('refused','refused_then_disconnect'):body.update(code=10601,state='stoped',msg='not for ODIN')
            if outcome=='code_200':body.update(code=200,state='printing')
            if outcome=='report_with_unusable_code':body.update(code='10601',state='x'*99)
            self.on_message(self,None,SimpleNamespace(topic='anycubic/anycubicCloud/v1/printer/public/20025/fixture-device/print/report',payload=json.dumps(body)))
            return SimpleNamespace(rc=1 if outcome=='publish_rejected' else 0,is_published=lambda:outcome!='publish_timeout')
        def loop(self,**kw):
            if outcome in ('disconnect_after_publish','refused_then_disconnect'):raise RuntimeError('synthetic disconnected')
            return 1  # No report/disconnected, never starts a retry.
        def disconnect(self):pass
    monkeypatch.setattr(mqtt,'Client',Client)
    monkeypatch.setattr(module.ssl,'SSLContext',lambda *a:SimpleNamespace(load_cert_chain=lambda *a:None))
    monkeypatch.setattr(webhook_utils,'device_connect_ip',lambda *a:'192.0.2.1')
    p=module.AnycubicLANPrinter('192.0.2.1');p.model_id='20025'
    def handshake():
        if outcome=='handshake_failed':raise RuntimeError('synthetic failure')
        if outcome=='other_model':p.model_id='20029'
        return dict(broker='mqtts://192.0.2.1:9883',deviceId='fixture-device',username='fixture',password='fixture',devicecrt='fixture',devicepk='fixture')
    monkeypatch.setattr(p,'handshake',handshake)
    rechecked=[]
    result=p.send_start(dict(type='print',action='start',msgid='fixture',data={}),before_publish=lambda:rechecked.append(True),timeout=.01)
    assert len(calls)==(0 if outcome in ('handshake_failed','other_model') else 1)
    assert result['published']==(outcome in ('correlated','no_report','disconnect_after_publish','refused','refused_then_disconnect','report_without_code','report_with_unusable_code','code_200'))
    assert bool(result.get('rejected'))==(outcome in ('refused','refused_then_disconnect'))
    if result.get('rejected'):
        assert (result['code'],result['state'])==(10601,'stoped') and 'not for ODIN' not in json.dumps(result)
    if outcome in ('report_without_code','report_with_unusable_code','code_200'):
        assert result['report_received'] is True and 'x'*99 not in json.dumps(result)
    if calls:
        assert rechecked==[True] and calls[0][1]=={'qos':0,'retain':False}
    if outcome=='correlated':assert result['report_received'] is True
    if outcome=='no_report':assert result['report_received'] is False
    if outcome=='publish_timeout':assert result['uncertain'] is True
    if outcome in ('publish_rejected','handshake_failed','other_model'):assert result['uncertain'] is False


def test_start_method_denies_other_actions():
    from modules.printers.adapters.anycubic_lan import AnycubicLANPrinter
    for kind,action in [('print','stop'),('info','query'),('print','resume')]:
        with pytest.raises(ValueError):
            AnycubicLANPrinter('192.0.2.1').send_start(dict(type=kind,action=action),before_publish=lambda:None)


from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch


def _bed_clear_request(engine, job_id, printer_id):
    """A confirmed Start next request, as start_next.start() records it before dispatch."""
    from core.durable_config import insert
    from modules.printers import start_next
    key = f'start_next_request:test-{job_id}'
    with engine.begin() as c:
        record = {'actor': {'user_id': 1, 'token_id': None}, 'printer_id': printer_id, 'job_id': job_id,
                  'fingerprint': 'fixture', 'attestation': 'owner_in_app',
                  'result': {'success': False, 'state': 'preparing', 'job_id': job_id, 'request_id': 'fixture'},
                  'admission_facts': start_next.admission_facts(c, job_id, printer_id)}
        insert(c, key, record)
    return start_next._context.set({'key': key, 'record': record, 'user': {}})


@pytest.mark.parametrize('scenario', ['sent','no_bed_clear','busy','flagged','flagged_after_upload','changed_slot','unknown_delivery','bad_handshake','not_idle','not_ready','ace_unknown','upload_failed','device_unavailable','file_changed','color_changed','duplicate_evidence','start_rejected','device_copy_short','device_copy_incomplete','device_copy_changing'])
def test_real_dispatch_pipeline_claims_and_no_retry(review_db,tmp_path,monkeypatch,scenario):
    from modules.printers import dispatch
    from modules.printers.adapters import moonraker, anycubic_lan
    from modules.printers.kobra_s1 import parse_ace
    engine,path=configure_dispatch(review_db,tmp_path,monkeypatch)
    path.write_text(S1_HEADER + '; filament_colour = #00AE42\n; filament_type = PLA\nG28\n')
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET model='Kobra S1',slot_count=4 WHERE id=59"))
        c.execute(text('INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,2,\'PLA\')'))
        c.execute(text("UPDATE jobs SET ams_mapping='[1]' WHERE id=53"))
        if scenario=='flagged':
            c.execute(text("UPDATE filament_slots SET do_not_feed=true,do_not_feed_note='Fault' WHERE printer_id=59 AND slot_number=2"))
    reads=[];uploads=[];publishes=[]
    def status(self):
        ace=parse_ace(HUB)
        reads.append(True)
        if scenario=='changed_slot' and len(reads)>1:ace['slots'][1]['type']='PETG'
        if scenario=='color_changed' and len(reads)>1:ace['slots'][1]['color']=[1,2,3]
        if scenario=='ace_unknown':ace=None
        return SimpleNamespace(internal_state='PRINTING' if scenario=='not_idle' else 'IDLE',raw_print_state='complete',raw_data={'webhooks':{'state':'error' if scenario=='not_ready' else 'ready'}},ace_status=ace)
    def upload(self,*a):
        uploads.append(a)
        if scenario=='flagged_after_upload':
            with engine.begin() as c:c.execute(text("UPDATE filament_slots SET do_not_feed=true,do_not_feed_note='Fault' WHERE printer_id=59 AND slot_number=2"))
        self.last_upload_busy=scenario=='busy'
        return scenario not in ('busy','upload_failed')
    def identity(self,name):
        if scenario=='device_unavailable':raise OSError('synthetic read failure')
        if scenario=='device_copy_incomplete':raise S1Refusal('s1_device_file_incomplete','Device file read did not match its declared size')
        if scenario=='device_copy_changing':raise S1Refusal('s1_device_file_changed','Device file changed while it was being read')
        return dict(path=name,md5='b'*32,size=1 if scenario=='device_copy_short' else 999,filaments=FILAMENTS if scenario=='file_changed' else FILAMENTS[:1])
    def send(self,payload,*,before_publish):
        if scenario=='bad_handshake':return dict(published=False,uncertain=False,message='Handshake failed')
        before_publish()
        if scenario=='duplicate_evidence':before_publish()
        publishes.append(payload)
        if scenario=='unknown_delivery':return dict(published=False,uncertain=True,message='Unknown delivery')
        if scenario=='start_rejected':return dict(published=True,rejected=True,code=10601,state='stoped',message='Printer refused the start (code 10601, state stoped); do not retry automatically')
        return dict(published=True,message='Published; no correlated report. Do not retry automatically.')
    monkeypatch.setattr(moonraker.MoonrakerPrinter,'get_status',status)
    monkeypatch.setattr(moonraker.MoonrakerPrinter,'upload_file',upload)
    monkeypatch.setattr(moonraker.MoonrakerPrinter,'device_file_identity',identity)
    monkeypatch.setattr(moonraker.MoonrakerPrinter,'start_print',lambda *a:pytest.fail('Plain Moonraker start'))
    monkeypatch.setattr(anycubic_lan.AnycubicLANPrinter,'send_start',send)
    from modules.printers import start_next
    marker=None if scenario=='no_bed_clear' else _bed_clear_request(engine,53,59)
    try:result=dispatch.dispatch_job(59,53)
    finally:
        if marker is not None:start_next._context.reset(marker)
    if scenario=='no_bed_clear':
        assert result[0] is False and 's1_bed_clear_required' in result[1]
        assert uploads==[] and publishes==[]
        return
    if scenario=='sent':
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM job_timeline_events WHERE job_id=53 AND printer_id=59 AND kind='bed_clear_confirmed'")).scalar_one()==1
    with engine.connect() as c:
        claim=c.execute(text('SELECT token,state FROM printer_dispatch_claims')).first()
        jobstate=c.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()
        assert jobstate==('printing' if scenario=='sent' else 'scheduled')
        if scenario in ('sent','unknown_delivery'):
            assert claim.state==('accepted' if scenario=='sent' else 'uncertain')
            from modules.printers.start_next import load
            facts=load(c,'s1_start:'+claim.token)
            assert facts['md5']=='b'*32 and facts['auto_refill']==1 and facts['warnings']
            assert facts['ams_box_mapping'][0]['ams_index']==1
            assert all(v==0 for v in facts['task_settings'].values())
        else:assert claim is None
    if scenario=='sent':
        assert result[0] is True and len(publishes)==1
        assert 'Published; no correlated report' in result[1] and 'auto_refill' in result[1], result
    else:
        assert result[0] is False
        code={'busy':'printer_busy','flagged':'slot_do_not_feed','flagged_after_upload':'slot_do_not_feed',
              'changed_slot':'s1_material_mismatch','bad_handshake':'s1_send_failed',
              'not_idle':'s1_printer_not_ready','not_ready':'s1_printer_not_ready',
              'ace_unknown':'s1_ace_unknown','upload_failed':'s1_upload_failed',
              'device_unavailable':'s1_device_file_unavailable','file_changed':'s1_file_changed',
              'color_changed':'s1_slot_changed','duplicate_evidence':'s1_attempt_already_recorded','start_rejected':'s1_start_rejected',
              'device_copy_short':'s1_device_file_incomplete','device_copy_incomplete':'s1_device_file_incomplete','device_copy_changing':'s1_device_file_changed'}.get(scenario)
        if code:assert code in result[1]
        assert len(publishes)==(1 if scenario in ('unknown_delivery','start_rejected') else 0)
    if scenario=='unknown_delivery':
        assert dispatch.dispatch_job(59,53)[0] is False
        assert len(publishes)==1 and len(uploads)==1


@pytest.mark.parametrize('role,status_code',[('operator',403),('viewer',403),('admin',200)])
def test_feed_flag_http_permissions_and_payload(inventory, monkeypatch, role, status_code):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from modules.printers import routes_crud
    from core.db import get_db
    from core.rbac import require_action
    app=FastAPI();app.include_router(routes_crud.router)
    route=next(r for r in app.routes if getattr(r,'path','')=='/printers/{printer_id}/slots/{slot_number}')
    actor=dict(id=1,role=role,group_id=None,is_superadmin=True)
    for dep in route.dependant.dependencies:
        if dep.call is not get_db:app.dependency_overrides[dep.call]=lambda:actor
    def db():
        with Session(inventory) as session:yield session
    app.dependency_overrides[get_db]=db
    monkeypatch.setattr(routes_crud,'ensure_local_filaments',lambda *a:pytest.fail('Flag edit must not contact inventory service'))
    with TestClient(app) as client:
        response=client.patch('/printers/1/slots/1',json=dict(do_not_feed=False,do_not_feed_note='Feed test passed'))
    assert response.status_code==status_code,response.text
    if role=='admin':
        assert response.json()['do_not_feed'] is False
        assert response.json()['do_not_feed_note']=='Feed test passed'
        assert response.json()['assigned_spool_id']==1
    with inventory.connect() as c:
        audits=c.execute(text("SELECT details FROM audit_logs WHERE action='filament_slot.feed_policy'")).scalars().all()
    assert len(audits)==(1 if role=='admin' else 0)
    if role=='admin':
        details=audits[0] if isinstance(audits[0],dict) else json.loads(audits[0])
        assert details['before']=={'do_not_feed':True,'note':'Feed fault'}
        assert details['after']=={'do_not_feed':False,'note':'Feed test passed'} and details['note']=='Feed test passed'


def test_s1_mapping_api_and_suggestions_skip_flagged(inventory):
    from sqlalchemy.orm import Session
    from core.ams_mapping import validate_for_printer,preview
    from fastapi import HTTPException
    from modules.printers.monitors.ams_inventory import sync_ace_inventory
    with inventory.begin() as c:sync_ace_inventory(c,1,parse_ace(HUB))
    with Session(inventory) as db:
        actor={'role':'admin','group_id':None}
        file=json.dumps([{'slot':1,'type':'PLA','color':'#00BB31'}])
        assert validate_for_printer(db,[{'source_slot':1,'target_slot':2}],file,1,actor)==[1]
        with pytest.raises(HTTPException,match='slot_do_not_feed'):
            validate_for_printer(db,[{'source_slot':1,'target_slot':1}],file,1,actor)
        p=preview(db,file,1,actor)
        assert p['available'][0]['do_not_feed'] is True
        assert p['suggested_mapping'][0]['target_slot']!=1


def test_unknown_identity_plain_start_and_automatic_dispatch_refuse(monkeypatch):
    from modules.printers import dispatch
    body = json.loads((FIXTURES / 'filament_hub.json').read_text())
    # No stored model: Rinkhals' ACE object or an unreadable probe refuses a plain start.
    for answer in (body, None, {'result': {}}):
        p=MoonrakerPrinter('192.0.2.1')
        monkeypatch.setattr(p,'_get',lambda path, answer=answer: answer)
        monkeypatch.setattr(p,'_post',lambda *a:pytest.fail('Unidentified ACE printer must not start'))
        assert p.start_print('test.gcode') is False
    # A generic Klipper printer without a stored model still starts.
    p=MoonrakerPrinter('192.0.2.1')
    seen=[]
    monkeypatch.setattr(p,'_get',lambda path: seen.append(path) or {'result': {'eventtime': 1.0, 'status': {}}})
    monkeypatch.setattr(p,'_post',lambda path, *a: seen.append(path) or {'result': 'ok'})
    assert p.start_print('test.gcode') is True
    assert seen == ['/printer/objects/query?filament_hub', '/printer/print/start?filename=test.gcode']
    monkeypatch.setenv('AUTO_DISPATCH','true')
    monkeypatch.setattr(dispatch,'_get_printer_info',lambda *a:dict(api_type='moonraker',model='Kobra S1'))
    monkeypatch.setattr(dispatch,'dispatch_job',lambda *a:pytest.fail('Automatic S1 dispatch forbidden'))
    monkeypatch.setattr(dispatch,'_get_next_scheduled_job',lambda *a:pytest.fail('Automatic S1 selection forbidden'))
    assert dispatch.attempt_dispatch(1) is None


@pytest.mark.parametrize('header', ['', '; filament_type = PLA', '; filament_colour = red\n; filament_type = PLA'])
def test_unknown_file_headers_refuse(header):
    with pytest.raises(S1Refusal,match='s1_file_filaments_unknown'):parse_header(header)


@pytest.mark.parametrize('change', [{'path':'../escape.gcode'}, {'md5':'bad'}, {'size':0}])
def test_invalid_device_file_identity(change):
    file=dict(path='test.gcode',md5='a'*32,size=1,filaments=FILAMENTS[:1]);file.update(change)
    with pytest.raises(S1Refusal,match='s1_file_identity_invalid'):
        build_payload(file,parse_ace(HUB)['slots'],[1],0,'fixture')


def test_idle_capture_contains_no_identity_and_replays():
    from modules.printers.parsing.anycubic_lan import parse_report
    payload=json.loads((FIXTURES/'idle_info_report.json').read_text())
    state=parse_report('anycubic/anycubicCloud/v1/printer/public/fixture-model/fixture-device/info/report',payload,{})
    assert 'ip' not in state and 'urls' not in state


def test_s1_gate_requires_admission_and_current_target(monkeypatch):
    from modules.printers import s1_dispatch
    assert s1_dispatch.dispatch(1, 'unused', 'unused', {}, {}, None) == (False, 's1_gate_required: S1 dispatch requires admitted authority')
    gate=s1_dispatch.create_start_gate(None, admission_inputs={'job':{},'token':'fixture'}, target_handle=SimpleNamespace(held=lambda:False))
    assert gate() is False and gate.refusal.code=='s1_target_changed'


def test_s1_missing_payload_has_named_refusal(tmp_path):
    from modules.printers.dispatch_options import _validated_print_options
    options, reason=_validated_print_options({'printer_id':1}, {'api_type':'moonraker','model':'Kobra S1'},str(tmp_path/'absent.gcode'),'a'*64)
    assert options is None and reason.startswith('s1_file_unavailable:')


def test_ace_persists_on_change_or_slot_cadence_not_every_poll(monkeypatch):
    from modules.printers.monitors import moonraker_monitor as mm
    monitor = object.__new__(mm.MoonrakerMonitor)
    writes = []
    monkeypatch.setattr(monitor, '_sync_ace', lambda ace: writes.append(ace) or True, raising=False)
    ace = parse_ace(HUB)
    monitor._maybe_sync_ace(ace, 100.0)
    monitor._maybe_sync_ace(dict(ace), 103.0)          # unchanged poll 3 s later
    assert len(writes) == 1
    changed = dict(ace, current_filament='0-1')
    monitor._maybe_sync_ace(changed, 106.0)            # change persists at once
    monitor._maybe_sync_ace(changed, 106.0 + mm.SLOT_SYNC_INTERVAL)  # regular cadence
    assert len(writes) == 3
    monkeypatch.setattr(monitor, '_sync_ace', lambda ace: False, raising=False)
    monitor._maybe_sync_ace(ace, 300.0)                # failed write retries next poll
    monitor._maybe_sync_ace(ace, 303.0)
    assert monitor._last_ace == changed



def _benchy_like(**over):
    fields = {'curr_bed_type': 'Textured PEI Plate', 'textured_plate_temp_initial_layer': '55',
              'hot_plate_temp_initial_layer': '55'}
    fields.update(over)
    body = ''.join(f'; {k} = {v}\n' for k, v in fields.items() if v is not None)
    return '; HEADER_BLOCK_START\n; BambuStudio 02.08.02.61\n; HEADER_BLOCK_END\n' + body


def test_start_metadata_accepts_proven_bambu_studio_files():
    from modules.printers.kobra_s1 import require_start_metadata
    assert require_start_metadata(_benchy_like()) == 55
    assert require_start_metadata(_benchy_like(curr_bed_type='High Temp Plate', hot_plate_temp_initial_layer='60,60')) == 60
    # make_first_layer.py v2: explicit first-layer key.
    v2 = '; HEADER_BLOCK_START\n; BambuStudio 02.08.02.61\n; first_layer_bed_temperature = 55\n'
    assert require_start_metadata(v2) == 55


@pytest.mark.parametrize('text', [
    # Oct 28 v1 corner test: no slicer header, gklib heated the bed to 5 C and levelled cold.
    '; HEADER_BLOCK_START\n; generated by make_first_layer.py\n; filament_colour = #6558B1\n; filament_type = PLA\nG28\n',
    '; HEADER_BLOCK_START\n; PrusaSlicer 2.8\n; first_layer_bed_temperature = 55\n',
    _benchy_like(textured_plate_temp_initial_layer='0'),
    _benchy_like(curr_bed_type='Mystery Plate'),
    _benchy_like(curr_bed_type=None, textured_plate_temp_initial_layer=None, hot_plate_temp_initial_layer=None),
])
def test_start_metadata_refuses_cold_bed_files(text):
    from modules.printers.kobra_s1 import require_start_metadata
    with pytest.raises(S1Refusal, match='s1_file_start_metadata_missing'):
        require_start_metadata(text)


def test_bed_clear_binding_is_per_request_and_claim(review_db, tmp_path, monkeypatch):
    from modules.printers import start_next
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    job = {'id': 53, 'printer_id': 59}
    with engine.connect() as c:
        assert start_next.bed_clear_bound(c, token='t1', job=job, admitted=False) is False  # no Start next request
    marker = _bed_clear_request(engine, 53, 59)
    try:
        with engine.begin() as c:
            assert start_next.bed_clear_bound(c, token='t1', job=job, admitted=False) is True
            assert start_next.bed_clear_bound(c, token='t1', job={'id': 54, 'printer_id': 59}, admitted=False) is False
            current = start_next.load(c, start_next._context.get()['key'])
            current['claim_token'] = 't1'
            start_next.save(c, start_next._context.get()['key'], current)
            assert start_next.bed_clear_bound(c, token='t1', job=job, admitted=True) is True
            assert start_next.bed_clear_bound(c, token='t2', job=job, admitted=True) is False
            assert start_next.bed_clear_bound(c, token='t2', job=job, admitted=False) is False  # already claimed
    finally:
        start_next._context.reset(marker)


def test_feed_restriction_survives_slot_count_and_monitor_cleanup(review_db, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy.orm import Session
    from modules.printers.routes_crud import update_printer
    from modules.printers.schemas import PrinterUpdate
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET slot_count=4 WHERE id=59"))
        c.execute(text('DELETE FROM filament_slots WHERE printer_id=59'))
        for n in range(1, 5):
            c.execute(text('INSERT INTO filament_slots(printer_id,slot_number,filament_type,do_not_feed) VALUES(59,:n,\'PLA\',:f)'), {'n': n, 'f': n == 2})
    with Session(engine) as db:
        with pytest.raises(HTTPException) as refused:
            update_printer(59, PrinterUpdate(slot_count=1), current_user={'id': 1, 'role': 'admin'}, db=db)
        assert refused.value.status_code == 409 and 'Slot 2' in str(refused.value.detail)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM filament_slots WHERE printer_id=59')).scalar_one() == 4
    from modules.printers.monitors import moonraker_monitor as mm
    monitor = object.__new__(mm.MoonrakerMonitor)
    monitor.printer_id, monitor.name = 59, 'S1'
    monkeypatch.setattr(mm, 'engine', engine)
    monitor._sync_filament_slots([SimpleNamespace(gate=0, material='PLA', color_hex='00AE42', name=None, gate_status=1)])
    with engine.connect() as c:
        rows = c.execute(text('SELECT slot_number,do_not_feed FROM filament_slots WHERE printer_id=59 ORDER BY slot_number')).all()
    assert [r[0] for r in rows] == [1, 2] and bool(rows[1][1]) is True


from tests.test_contracts.test_profile_library_behavior import profile_client, token_client


@pytest.mark.parametrize('gcode', ['SDCARD_PRINT_FILE FILENAME=odin.gcode', 'M24', 'SET_PRESSURE_ADVANCE ADVANCE=0.04'])
def test_profiles_never_reach_a_kobra_s1(profile_client, review_db, gcode):
    """Any G-code or macro can start a print; S1 starts only through Start next."""
    client, issue, calls, _ = profile_client
    review_db.execute(text("UPDATE printers SET model='Kobra S1' WHERE id=9"))
    review_db.execute(text("UPDATE printer_profiles SET category='custom',raw_content=:content WHERE id=101"),
                      {'content': json.dumps({'gcode': gcode})})
    review_db.commit()
    response = client.post('/profiles/101/apply', headers=issue(['read:models', 'write:printers']), json={'printer_id': 9})
    assert response.status_code == 400 and 'Kobra S1' in response.json()['detail']
    assert calls == []
    assert review_db.execute(text('SELECT last_applied_at FROM printer_profiles WHERE id=101')).scalar_one() is None


def test_profile_apply_rechecks_s1_identity_before_sending():
    from fastapi import HTTPException
    from modules.system.profile_routes import _apply_profile_commands
    printer = SimpleNamespace(api_type='moonraker', model='Kobra S1', machine_type=None, api_host='192.0.2.1', name='S1')
    handle = SimpleNamespace(held=lambda: pytest.fail('S1 refusal precedes any target use'))
    with pytest.raises(HTTPException) as refused:
        _apply_profile_commands(None, 101, 9, None, printer, ['M24'], handle, {}, None, None)
    assert refused.value.status_code == 400


def test_manual_ams_sync_keeps_flagged_slots_beyond_gate_count(review_db, tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    from modules.printers import routes_ams
    from modules.printers.adapters import moonraker
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as c:
        c.execute(text('DELETE FROM filament_slots WHERE printer_id=59'))
        for n in range(1, 5):
            c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,do_not_feed) VALUES(59,:n,'PLA',:f)"), {'n': n, 'f': n == 3})
    gate = moonraker.MoonrakerFilamentSlot(gate=0, material='PLA', color_hex='00AE42', loaded=True, gate_status=1)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'connect', lambda self: True)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'get_status', lambda self: SimpleNamespace(filament_slots=[gate], ace_status=None))
    monkeypatch.setattr(routes_ams, '_lock_ams_mutation', lambda db, pid, *a: (db.get(routes_ams.Printer, pid), None), raising=False)
    with Session(engine) as db:
        routes_ams.sync_ams_state(59, current_user={'id': 1, 'role': 'admin'}, db=db)
    with engine.connect() as c:
        rows = c.execute(text('SELECT slot_number,do_not_feed FROM filament_slots WHERE printer_id=59 ORDER BY slot_number')).all()
    assert [r[0] for r in rows] == [1, 3] and bool(rows[1][1]) is True


def test_slot_count_reduction_locks_printer_before_reading_flags(review_db, tmp_path, monkeypatch):
    """The flag update and the reduction serialize on the printer row; flags are read under it."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from modules.printers.routes_crud import update_printer
    from modules.printers.schemas import PrinterUpdate
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET slot_count=4 WHERE id=59"))
        c.execute(text('DELETE FROM filament_slots WHERE printer_id=59'))
        for n in range(1, 5):
            c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,:n,'PLA')"), {'n': n})
    seen = []
    listener = lambda conn, cursor, statement, *a: seen.append(' '.join(statement.split()).upper())
    event.listen(engine, 'before_cursor_execute', listener)
    try:
        with Session(engine) as db:
            update_printer(59, PrinterUpdate(slot_count=2), current_user={'id': 1, 'role': 'admin'}, db=db)
    finally:
        event.remove(engine, 'before_cursor_execute', listener)
    lock = next(i for i, q in enumerate(seen) if q.startswith('UPDATE PRINTERS SET NAME=NAME'))
    reread = next(i for i, q in enumerate(seen) if i > lock and q.startswith('SELECT') and 'FROM FILAMENT_SLOTS' in q)
    delete = next(i for i, q in enumerate(seen) if q.startswith('DELETE FROM FILAMENT_SLOTS'))
    assert lock < reread < delete
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM filament_slots WHERE printer_id=59')).scalar_one() == 2


def test_printer_delete_refuses_while_a_slot_is_flagged(review_db, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy.orm import Session
    from core.registry import registry
    from modules.printers.routes_crud import delete_printer
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    policy = SimpleNamespace(assert_printer_tenant_change_or_delete_allowed=lambda db, pid: None)
    original = registry.get_provider
    monkeypatch.setattr(registry, 'get_provider', lambda name: policy if name == 'EducationPolicyProvider' else original(name))
    with engine.begin() as c:
        c.execute(text('DELETE FROM filament_slots WHERE printer_id=59'))
        c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,do_not_feed,do_not_feed_note) VALUES(59,1,'PLA',true,'Faulty green')"))
    with Session(engine) as db:
        with pytest.raises(HTTPException) as refused:
            delete_printer(59, current_user={'id': 1, 'role': 'admin'}, db=db)
        assert refused.value.status_code == 409 and 'Slot 1' in str(refused.value.detail)
        db.rollback()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM printers WHERE id=59')).scalar_one() == 1
        assert c.execute(text('SELECT do_not_feed FROM filament_slots WHERE printer_id=59')).scalar_one()


@pytest.mark.parametrize('mapping,code', [([1, 1], 's1_mapping_duplicate_slot'), ([1, -1], 's1_mapping_incomplete'),
                                          ([-1, 1], 's1_mapping_incomplete'), ([1], 's1_mapping_incomplete')])
def test_s1_mapping_stays_stricter_than_the_multi_source_rules(mapping, code):
    """2.0.36 lets Bambu sources share a tray or skip one. Neither may reach gklib."""
    from core.print_payload import validate_mapping
    slots = parse_ace(HUB)['slots']
    with pytest.raises(S1Refusal, match=code):
        verify_mapping(FILAMENTS, slots, mapping)
    with pytest.raises(S1Refusal, match=code):
        build_payload(dict(path='fixture.gcode', size=1, md5='a' * 32, filaments=FILAMENTS), slots, mapping, 1, 'fixture')
    # The shared validator now accepts a shared tray, so the S1 rule is what refuses it.
    assert validate_mapping([dict(source_slot=1, target_slot=2), dict(source_slot=2, target_slot=2)],
                            json.dumps([dict(slot=1, type='PLA'), dict(slot=2, type='PLA')]), {2: 'PLA'}, 'bambu') == [1, 1]


def test_s1_mapping_api_refuses_a_shared_ace_slot_and_bambu_keeps_it(inventory):
    from sqlalchemy.orm import Session
    from core.ams_mapping import validate_for_printer
    from fastapi import HTTPException
    from modules.printers.monitors.ams_inventory import sync_ace_inventory
    with inventory.begin() as c:
        sync_ace_inventory(c, 1, parse_ace(HUB))
        c.execute(text("INSERT INTO printers(id,name,api_type,model) VALUES(2,'Bambu','bambu','A1')"))
        c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(2,2,'PLA')"))
    file = json.dumps([{'slot': 1, 'type': 'PLA'}, {'slot': 2, 'type': 'PLA'}])
    shared = [{'source_slot': 1, 'target_slot': 2}, {'source_slot': 2, 'target_slot': 2}]
    actor = {'role': 'admin', 'group_id': None}
    with Session(inventory) as db:
        with pytest.raises(HTTPException, match='separate ACE slot'):
            validate_for_printer(db, shared, file, 1, actor)
        assert validate_for_printer(db, [{'source_slot': 1, 'target_slot': 2}, {'source_slot': 2, 'target_slot': 3}], file, 1, actor) == [1, 2]
        assert validate_for_printer(db, shared, file, 2, actor) == [1, 1]


def test_s1_job_sharing_an_ace_slot_refuses_before_any_device_contact(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch
    from modules.printers.adapters import moonraker, anycubic_lan
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch)
    path.write_text(S1_HEADER + '; filament_colour = #00AE42;#FF00FF\n; filament_type = PLA;PLA\nG28\n')
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET model='Kobra S1',slot_count=4 WHERE id=59"))
        c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,2,'PLA')"))
        c.execute(text("UPDATE jobs SET ams_mapping='[1,1]' WHERE id=53"))
    def forbidden(*a, **k):
        pytest.fail('A shared-slot S1 job must stop before device contact')
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'get_status', forbidden)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'upload_file', forbidden)
    monkeypatch.setattr(anycubic_lan.AnycubicLANPrinter, 'send_start', forbidden)
    result = dispatch.dispatch_job(59, 53)
    assert result[0] is False and 's1_mapping_duplicate_slot' in result[1]
    with engine.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one() == 0


def test_s1_identity_needs_moonraker_so_gates_cannot_cross():
    assert not is_kobra_s1(dict(api_type='bambu', model='Kobra S1'))
    assert not is_kobra_s1(dict(api_type='prusalink', machine_type='Kobra S1'))


def test_s1_start_never_enters_the_generic_ams_gate(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, dispatch_ams_gate, services, start_next
    from modules.printers.adapters import moonraker, anycubic_lan
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch)
    path.write_text(S1_HEADER + '; filament_colour = #00AE42\n; filament_type = PLA\nG28\n')
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET model='Kobra S1',slot_count=4 WHERE id=59"))
        c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,2,'PLA')"))
        c.execute(text("UPDATE jobs SET ams_mapping='[1]' WHERE id=53"))
    def forbidden(*a, **k):
        pytest.fail('The generic AMS gate must not judge an S1 start')
    monkeypatch.setattr(dispatch_ams_gate, 'create_ams_start_gate', forbidden)
    monkeypatch.setattr(dispatch_ams_gate, 'preflight_ams_source', forbidden)
    monkeypatch.setattr(services, 'inspect_ams_payload', forbidden)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'get_status', lambda self: SimpleNamespace(
        internal_state='IDLE', raw_print_state='complete', raw_data={'webhooks': {'state': 'ready'}}, ace_status=parse_ace(HUB)))
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'upload_file', lambda self, *a: True)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'device_file_identity', lambda self, name: dict(path=name, md5='b' * 32, size=999, filaments=FILAMENTS[:1]))
    publishes = []
    def send(self, payload, *, before_publish):
        before_publish()
        publishes.append(payload)
        return dict(published=True, message='Published; no correlated report. Do not retry automatically.')
    monkeypatch.setattr(anycubic_lan.AnycubicLANPrinter, 'send_start', send)
    marker = _bed_clear_request(engine, 53, 59)
    try:
        result = dispatch.dispatch_job(59, 53)
    finally:
        start_next._context.reset(marker)
    assert result[0] is True, result
    assert [m['ams_index'] for m in publishes[0]['data']['ams_settings']['ams_box_mapping']] == [1]


def test_bambu_multi_source_start_never_enters_the_s1_path(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, s1_dispatch
    from tests.test_contracts.test_multi_source_ams_dispatch import setup_multi, transport, owned_read
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch)
    def forbidden(*a, **k):
        pytest.fail('A Bambu start must not enter the S1 path')
    monkeypatch.setattr(s1_dispatch, 'create_start_gate', forbidden)
    monkeypatch.setattr(s1_dispatch, 'options', forbidden)
    monkeypatch.setattr(s1_dispatch, 'dispatch', forbidden)
    events = []
    sent = transport(monkeypatch, path, 'legacy', events)
    owned_read(monkeypatch, engine, report, events)
    result = dispatch.dispatch_job(59, 53)
    assert result[0] is True, result
    assert events == ['upload', 'fresh_read', 'start'] and sent[0]['ams_mapping'] == [2, 1]


def _operator_complete(review_db, monkeypatch, *, model, mapping, grams=(12,)):
    """Slots 1-3 hold spools 101-103 (100 g each). Returns their weights and the unattributed audits."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from core import db as core_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    engine = review_db.get_bind()
    materials = [{'slot': i + 1, 'type': 'PLA', 'used_grams': amount} for i, amount in enumerate(grams)]
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET api_type='moonraker',model=:model,slot_count=4 WHERE id=9"), {'model': model})
        conn.execute(text("INSERT INTO models(id,name,org_id,color_requirements) VALUES(101,'fixture',1,:requirements)"),
                     {'requirements': json.dumps({'slot1': {'grams': grams[0]}})})
        conn.execute(text("INSERT INTO print_files(id,filename,org_id,filaments_json) VALUES(101,'fixture.gcode',1,:materials)"),
                     {'materials': json.dumps(materials)})
        conn.execute(text("INSERT INTO jobs(id,item_name,model_id,printer_id,status,charged_to_org_id,quantity,hold,print_file_id,ams_mapping) "
                          "VALUES(101,'fixture',101,9,'printing',1,1,0,101,:mapping)"), {'mapping': mapping})
        conn.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(101,'Synthetic','Test','PLA')"))
        conn.execute(text('INSERT INTO spools(id,filament_id,remaining_weight_g,initial_weight_g,org_id) '
                          'VALUES(101,101,100,100,1),(102,101,100,100,1),(103,101,100,100,1)'))
        conn.execute(text('UPDATE filament_slots SET assigned_spool_id=101 WHERE printer_id=9 AND slot_number=1'))
        conn.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,assigned_spool_id) "
                          "VALUES(9,2,'PLA',102),(9,3,'PLA',103),(9,4,'PLA',NULL)"))
    app = FastAPI()
    app.include_router(jobs_lifecycle.router)
    def request_db():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[core_db.get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'operator', 'group_id': 1, 'is_active': True}
    with TestClient(app) as client:
        response = client.post('/jobs/101/complete')
    assert response.status_code == 200, response.text
    with engine.connect() as conn:
        weights = conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (101,102,103) ORDER BY id')).scalars().all()
        audits = conn.execute(text("SELECT details FROM audit_logs WHERE action='filament.unattributed'")).scalars().all()
        notes = conn.execute(text('SELECT notes FROM jobs WHERE id=101')).scalar_one()
    return weights, [a if isinstance(a, dict) else json.loads(a) for a in audits], notes


@pytest.mark.parametrize('mapping,grams,weights,slots', [
    pytest.param('[1]', (12,), [100, 88, 100], ['Slot 2'], id='one_colour_slot_2'),
    pytest.param('[2,1]', (12, 5), [100, 95, 88], ['Slot 3', 'Slot 2'], id='two_colour_in_mapped_order'),
])
def test_s1_operator_complete_deducts_from_the_mapped_slot(review_db, monkeypatch, mapping, grams, weights, slots):
    actual, audits, notes = _operator_complete(review_db, monkeypatch, model='Kobra S1', mapping=mapping, grams=grams)
    assert actual == weights and audits == []
    assert all(slot in notes for slot in slots) and 'Slot 1' not in notes


@pytest.mark.parametrize('mapping,grams', [
    pytest.param(None, (12,), id='no_stored_mapping'),
    pytest.param('[]', (12,), id='empty_mapping'),
    pytest.param('[-1]', (12,), id='source_not_mapped'),
    pytest.param('[true]', (12,), id='not_a_slot_index'),
    pytest.param('[7]', (12,), id='no_such_slot'),
    pytest.param('[3]', (12,), id='mapped_slot_has_no_spool'),
    pytest.param('[1,-1]', (12, 5), id='one_of_two_sources_not_mapped'),
])
def test_s1_operator_complete_leaves_an_unresolved_mapping_unattributed(review_db, monkeypatch, mapping, grams):
    """Never fall back to file source N meaning slot N: on an S1 that is a guess."""
    weights, audits, notes = _operator_complete(review_db, monkeypatch, model='Kobra S1', mapping=mapping, grams=grams)
    assert weights == [100, 100, 100]
    assert len(audits) == 1 and audits[0]['consumption']
    assert 'Filament deducted' not in (notes or '')


def test_other_moonraker_printers_keep_source_slot_deduction(review_db, monkeypatch):
    weights, audits, notes = _operator_complete(review_db, monkeypatch, model='Voron 2.4', mapping=None)
    assert weights == [88, 100, 100] and audits == [] and 'Slot 1' in notes


class _ClosingServer:
    """A real HTTP server for the read-back, because http.client decides what a short read does."""
    def __init__(self, bodies, declared=None, send_length=True):
        import http.server, threading
        served = self.served = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = bodies[min(len(served), len(bodies) - 1)]
                served.append(self.path)
                self.send_response(200)
                if send_length:
                    self.send_header('Content-Length', str(declared if declared is not None else len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
            def log_message(self, *a):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()
    def printer(self):
        return MoonrakerPrinter('127.0.0.1', self.server.server_address[1], model='Kobra S1')


@pytest.fixture
def loopback_device(monkeypatch):
    # The real client against a real socket; only the device address policy is opened for loopback.
    import urllib.request
    from modules.printers.adapters import moonraker
    monkeypatch.setattr(moonraker, 'urlopen', urllib.request.urlopen)


DEVICE_FILE = (FIXTURES / 'bambu_studio_one_colour.gcode').read_bytes()


def test_read_back_hashes_the_complete_copy_twice(loopback_device):
    with _ClosingServer([DEVICE_FILE]) as server:
        facts = server.printer().device_file_identity('print.gcode')
    assert facts['md5'] == hashlib.md5(DEVICE_FILE, usedforsecurity=False).hexdigest() and facts['size'] == len(DEVICE_FILE)
    assert len(server.served) == 2


@pytest.mark.parametrize('kwargs,code', [
    pytest.param(dict(bodies=[DEVICE_FILE[:len(DEVICE_FILE) // 2]], declared=len(DEVICE_FILE)), 's1_device_file_incomplete', id='server_closes_at_half_of_content_length'),
    pytest.param(dict(bodies=[DEVICE_FILE], send_length=False), 's1_device_file_incomplete', id='no_content_length'),
    pytest.param(dict(bodies=[DEVICE_FILE, DEVICE_FILE + b'; patched while reading\n']), 's1_device_file_changed', id='changes_between_reads'),
])
def test_read_back_refuses_a_copy_it_cannot_trust(loopback_device, kwargs, code):
    with _ClosingServer(**kwargs) as server:
        with pytest.raises(S1Refusal, match=code):
            server.printer().device_file_identity('print.gcode')


def _suggestion_case(review_db, tmp_path, monkeypatch):
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET model='Kobra S1',slot_count=4 WHERE id=59"))
        c.execute(text("UPDATE jobs SET status='pending',printer_id=NULL,target_type=NULL,target_filter=NULL,priority=1,quantity=1 WHERE id=53"))
        c.execute(text("UPDATE jobs SET status='pending',printer_id=59,ams_mapping='[0]',priority=5,quantity=1 WHERE id=54"))
    return engine


def test_s1_suggestion_skips_unassigned_work_ahead_of_its_pinned_job(review_db, tmp_path, monkeypatch):
    """An unassigned job can't block the S1: Start next must match the suggestion."""
    from sqlalchemy.orm import Session
    from modules.jobs.completion_summary import eligible, next_suggestion
    from modules.jobs.models import Job
    from modules.printers.models import Printer
    engine = _suggestion_case(review_db, tmp_path, monkeypatch)
    with Session(engine) as db:
        printer = db.get(Printer, 59)
        assert not eligible(db, db.get(Job, 53), printer)
        assert eligible(db, db.get(Job, 54), printer)
        assert next_suggestion(db, printer)['job_id'] == 54
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET model='Voron 2.4' WHERE id=59"))
    with Session(engine) as db:
        assert eligible(db, db.get(Job, 53), db.get(Printer, 59)), 'Other printers can still adopt unassigned work'


def test_s1_start_next_refuses_an_unassigned_job_even_if_suggested(review_db, tmp_path, monkeypatch):
    import uuid
    from fastapi import HTTPException
    from sqlalchemy.orm import Session
    from modules.printers import start_next
    from modules.printers.models import Printer
    from modules.printers.routes_start_next import StartNext
    engine = _suggestion_case(review_db, tmp_path, monkeypatch)
    monkeypatch.setattr(start_next, 'next_suggestion', lambda *a, **k: {'job_id': 53, 'fingerprint': 'f' * 64})
    data = StartNext(suggested_job_id=53, suggestion_fingerprint='f' * 64, request_id=uuid.uuid4(), bed_clear_confirmed=True, attestation='owner_in_app')
    with Session(engine) as db:
        with pytest.raises(HTTPException) as refused:
            start_next.start(db, db.get(Printer, 59), {'id': 3, 'role': 'admin', 'group_id': 1}, data)
    assert refused.value.status_code == 409 and 'assigned' in str(refused.value.detail)
    with engine.connect() as c:
        assert c.execute(text('SELECT printer_id FROM jobs WHERE id=53')).scalar_one() is None


@pytest.mark.parametrize('model,refused', [('Kobra S1', True), ('Voron 2.4', False)])
def test_s1_start_next_needs_an_explicit_attestation(review_db, tmp_path, monkeypatch, model, refused):
    import uuid
    from fastapi import HTTPException
    from sqlalchemy.orm import Session
    from modules.printers import dispatch, start_next
    from modules.printers.models import Printer
    from modules.printers.routes_start_next import StartNext
    engine = _suggestion_case(review_db, tmp_path, monkeypatch)
    with engine.begin() as c:
        c.execute(text('UPDATE printers SET model=:model WHERE id=59'), {'model': model})
    monkeypatch.setattr(start_next, 'next_suggestion', lambda *a, **k: {'job_id': 54, 'fingerprint': 'f' * 64})
    monkeypatch.setattr(dispatch, 'dispatch_job', lambda *a: (False, 'Synthetic stop'))
    data = StartNext(suggested_job_id=54, suggestion_fingerprint='f' * 64, request_id=uuid.uuid4(), bed_clear_confirmed=True)
    with Session(engine) as db:
        printer = db.get(Printer, 59)
        if refused:
            with pytest.raises(HTTPException) as error:
                start_next.start(db, printer, {'id': 3, 'role': 'admin', 'group_id': 1}, data)
            assert error.value.status_code == 422
            data.attestation = 'owner_confirmed_to_operator'
        assert start_next.start(db, printer, {'id': 3, 'role': 'admin', 'group_id': 1}, data)['state'] == 'refused'
    with engine.connect() as c:
        stored = json.loads(c.execute(text("SELECT value FROM system_config WHERE key LIKE 'start_next:%'")).scalar_one())
    assert stored['attestation'] == ('owner_confirmed_to_operator' if refused else 'owner_in_app')


def test_s1_direct_dispatch_refuses_before_power_claim_or_device(review_db, tmp_path, monkeypatch):
    from core import dispatch_claims
    from modules.printers import dispatch, dispatch_acquisition, smart_plug
    from modules.printers.adapters import moonraker
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch)
    path.write_text(S1_HEADER + '; filament_colour = #00AE42\n; filament_type = PLA\nG28\n')
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET model='Kobra S1',slot_count=4 WHERE id=59"))
        c.execute(text("UPDATE jobs SET ams_mapping='[0]' WHERE id=53"))
    def forbidden(*a, **k):
        pytest.fail('An S1 outside Start next must stop before this')
    monkeypatch.setattr(smart_plug, 'prepare_bounded_dispatch_power', forbidden)
    monkeypatch.setattr(dispatch_claims, 'acquire', forbidden)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_status_is_idle', forbidden)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'get_status', forbidden)
    result = dispatch.dispatch_job(59, 53)
    assert result[0] is False and result[1].startswith('s1_bed_clear_required')


def test_manual_ace_sync_is_audited(review_db, tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    from modules.printers import routes_ams
    from modules.printers.adapters import moonraker
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET model='Kobra S1',slot_count=4 WHERE id=59"))
    ace = parse_ace(HUB)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'connect', lambda self: True)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'get_status', lambda self: SimpleNamespace(
        filament_slots=[moonraker.MoonrakerFilamentSlot(gate=0, material='PLA', color_hex='00AE42', loaded=True, gate_status=1)], ace_status=ace))
    monkeypatch.setattr(routes_ams, '_lock_ams_mutation', lambda db, pid, *a: (db.get(routes_ams.Printer, pid), None), raising=False)
    with Session(engine) as db:
        routes_ams.sync_ams_state(59, current_user={'id': 1, 'role': 'admin'}, db=db)
    with engine.connect() as c:
        audit = c.execute(text("SELECT entity_id,details FROM audit_logs WHERE action='sync' AND entity_type='printer'")).one()
    details = audit[1] if isinstance(audit[1], dict) else json.loads(audit[1])
    assert audit[0] == 59 and details == {'slots_synced': 4, 'source': 'moonraker_ace'}


@pytest.mark.parametrize('keys', [
    '; textured_plate_temp_initial_layer = 0\n; first_layer_bed_temperature = 60\n',
    '; textured_plate_temp_initial_layer = 0,55\n; first_layer_bed_temperature = 60\n',
])
def test_an_explicit_zero_on_the_selected_plate_is_a_cold_bed(keys):
    from modules.printers.kobra_s1 import require_start_metadata
    with pytest.raises(S1Refusal, match='s1_file_start_metadata_missing'):
        require_start_metadata('; BambuStudio 02.08.02.61\n; curr_bed_type = Textured PEI Plate\n' + keys)


def test_first_layer_key_only_answers_when_the_plate_has_no_key_of_its_own():
    from modules.printers.kobra_s1 import require_start_metadata
    assert require_start_metadata('; BambuStudio 02.08.02.61\n; curr_bed_type = Textured PEI Plate\n; first_layer_bed_temperature = 60\n') == 60


def test_kobra_s1_max_is_not_a_kobra_s1():
    for identity in ('Kobra S1 Max', 'kObRa s1 mAx', 'kobra_s1_max', 'Anycubic Kobra S1-Max'):
        assert not is_kobra_s1(dict(api_type='moonraker', model=identity))
        assert dispatch_unsupported_reason(dict(api_type='moonraker', model=identity)).startswith('printer_dispatch_unsupported')
    for identity in ('Anycubic Kobra S1 Combo', 'kobra_s1', 'kobra_s1_combo', 'Kobra-S1'):
        assert is_kobra_s1(dict(api_type='moonraker', model=identity)), identity
    assert not is_kobra_s1(dict(api_type='moonraker', model='Kobra S10'))
