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


def test_upload_409_is_busy_and_device_identity_hashes_readback(tmp_path, monkeypatch):
    from core import webhook_utils
    p=MoonrakerPrinter('192.0.2.1',model='Kobra S1')
    local=tmp_path/'print.gcode';local.write_bytes(b'; filament_colour = #00AE42\n; filament_type = PLA\nG28\n')
    monkeypatch.setattr(webhook_utils,'device_request',lambda *a,**k:SimpleNamespace(status_code=409))
    assert p.upload_file(str(local)) is False and p.last_upload_busy
    device=b'; Rinkhals added this line\n'+local.read_bytes()
    import io
    from modules.printers.adapters import moonraker
    monkeypatch.setattr(moonraker,'urlopen',lambda *a,**k:io.BytesIO(device))
    facts=p.device_file_identity('folder/print.gcode')
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
    from modules.printers.slot_feed_policy import flagged_mapping_reason,update_policy
    from core.models import AuditLog
    from fastapi import HTTPException
    with inventory.connect() as c:
        assert flagged_mapping_reason(c,1,[0])=='slot_do_not_feed: Slot 1: Feed fault'
        assert flagged_mapping_reason(c,1,[1]) is None
    with Session(inventory) as db:
        slot=db.get(FilamentSlot,1)
        for role in ['viewer','operator']:
            with pytest.raises(HTTPException) as e:
                update_policy(db,slot,{'do_not_feed':False},{'role':role})
            assert e.value.status_code==403
        update_policy(db,slot,{'do_not_feed':False,'do_not_feed_note':'Feed passed'},{'role':'admin'})
        slot.do_not_feed=False;slot.do_not_feed_note='Feed passed';db.commit()
        audit=db.query(AuditLog).filter_by(action='filament_slot.feed_policy').one()
        details=audit.details if isinstance(audit.details,dict) else json.loads(audit.details)
        assert details['before']=={'do_not_feed':True,'note':'Feed fault'}
        assert details['after']=={'do_not_feed':False,'note':'Feed passed'} and details['note']=='Feed passed'


@pytest.mark.parametrize('outcome', ['correlated','no_report','disconnect_after_publish','publish_rejected','publish_timeout','handshake_failed'])
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
            if outcome=='correlated':body['msgid']='fixture'
            self.on_message(self,None,SimpleNamespace(topic='anycubic/anycubicCloud/v1/printer/public/fixture-model/fixture-device/print/report',payload=json.dumps(body)))
            return SimpleNamespace(rc=1 if outcome=='publish_rejected' else 0,is_published=lambda:outcome!='publish_timeout')
        def loop(self,**kw):
            if outcome=='disconnect_after_publish':raise RuntimeError('synthetic disconnected')
            return 1  # No report/disconnected, never starts a retry.
        def disconnect(self):pass
    monkeypatch.setattr(mqtt,'Client',Client)
    monkeypatch.setattr(module.ssl,'SSLContext',lambda *a:SimpleNamespace(load_cert_chain=lambda *a:None))
    monkeypatch.setattr(webhook_utils,'device_connect_ip',lambda *a:'192.0.2.1')
    p=module.AnycubicLANPrinter('192.0.2.1');p.model_id='fixture-model'
    def handshake():
        if outcome=='handshake_failed':raise RuntimeError('synthetic failure')
        return dict(broker='mqtts://192.0.2.1:9883',deviceId='fixture-device',username='fixture',password='fixture',devicecrt='fixture',devicepk='fixture')
    monkeypatch.setattr(p,'handshake',handshake)
    rechecked=[]
    result=p.send_start(dict(type='print',action='start',msgid='fixture',data={}),before_publish=lambda:rechecked.append(True),timeout=.01)
    assert len(calls)==(0 if outcome=='handshake_failed' else 1)
    assert result['published']==(outcome in ('correlated','no_report','disconnect_after_publish'))
    if calls:
        assert rechecked==[True] and calls[0][1]=={'qos':0,'retain':False}
    if outcome=='correlated':assert result['report_received'] is True
    if outcome=='no_report':assert result['report_received'] is False
    if outcome=='publish_timeout':assert result['uncertain'] is True
    if outcome in ('publish_rejected','handshake_failed'):assert result['uncertain'] is False


def test_start_method_denies_other_actions():
    from modules.printers.adapters.anycubic_lan import AnycubicLANPrinter
    for kind,action in [('print','stop'),('info','query'),('print','resume')]:
        with pytest.raises(ValueError):
            AnycubicLANPrinter('192.0.2.1').send_start(dict(type=kind,action=action),before_publish=lambda:None)


from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch


@pytest.mark.parametrize('scenario', ['sent','busy','flagged','flagged_after_upload','changed_slot','unknown_delivery','bad_handshake','not_idle','not_ready','ace_unknown','upload_failed','device_unavailable','file_changed','color_changed','duplicate_evidence'])
def test_real_dispatch_pipeline_claims_and_no_retry(review_db,tmp_path,monkeypatch,scenario):
    from modules.printers import dispatch
    from modules.printers.adapters import moonraker, anycubic_lan
    from modules.printers.kobra_s1 import parse_ace
    engine,path=configure_dispatch(review_db,tmp_path,monkeypatch)
    path.write_text('; filament_colour = #00AE42\n; filament_type = PLA\nG28\n')
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
        return dict(path=name,md5='b'*32,size=999,filaments=FILAMENTS if scenario=='file_changed' else FILAMENTS[:1])
    def send(self,payload,*,before_publish):
        if scenario=='bad_handshake':return dict(published=False,uncertain=False,message='Handshake failed')
        before_publish()
        if scenario=='duplicate_evidence':before_publish()
        publishes.append(payload)
        if scenario=='unknown_delivery':return dict(published=False,uncertain=True,message='Unknown delivery')
        return dict(published=True,message='Published; no correlated report. Do not retry automatically.')
    monkeypatch.setattr(moonraker.MoonrakerPrinter,'get_status',status)
    monkeypatch.setattr(moonraker.MoonrakerPrinter,'upload_file',upload)
    monkeypatch.setattr(moonraker.MoonrakerPrinter,'device_file_identity',identity)
    monkeypatch.setattr(moonraker.MoonrakerPrinter,'start_print',lambda *a:pytest.fail('Plain Moonraker start'))
    monkeypatch.setattr(anycubic_lan.AnycubicLANPrinter,'send_start',send)
    result=dispatch.dispatch_job(59,53)
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
    if scenario=='sent':assert result[0] is True and len(publishes)==1
    else:
        assert result[0] is False
        code={'busy':'printer_busy','flagged':'slot_do_not_feed','flagged_after_upload':'slot_do_not_feed',
              'changed_slot':'s1_material_mismatch','bad_handshake':'s1_send_failed',
              'not_idle':'s1_printer_not_ready','not_ready':'s1_printer_not_ready',
              'ace_unknown':'s1_ace_unknown','upload_failed':'s1_upload_failed',
              'device_unavailable':'s1_device_file_unavailable','file_changed':'s1_file_changed',
              'color_changed':'s1_slot_changed','duplicate_evidence':'s1_attempt_already_recorded'}.get(scenario)
        if code:assert code in result[1]
        assert len(publishes)==(1 if scenario=='unknown_delivery' else 0)
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
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='filament_slot.feed_policy'")).scalar_one()==(1 if role=='admin' else 0)


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
