"""Production MQTT payload/configuration behavior without an external broker."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("enterprise_license")

@pytest.fixture
def republisher(review_db,monkeypatch):
    from modules.notifications import mqtt_republish
    monkeypatch.setattr(mqtt_republish,'engine',review_db.get_bind())
    monkeypatch.setattr(mqtt_republish,'_config_cache',None)
    monkeypatch.setattr(mqtt_republish,'_config_ts',0)
    monkeypatch.setattr(mqtt_republish,'_client',None)
    return mqtt_republish

@pytest.fixture
def configured(republisher,review_db):
    rows={'mqtt_republish_enabled':'true','mqtt_republish_host':'synthetic.invalid','mqtt_republish_port':'1883','mqtt_republish_topic_prefix':'synthetic///'}
    for key,value in rows.items():review_db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),{'key':key,'value':value})
    review_db.commit();return republisher

@pytest.mark.parametrize('kind',['telemetry','job','alert','fleet'])
def test_public_payloads_use_configured_topics_and_transport_flags(configured,monkeypatch,kind):
    calls=[]
    client=SimpleNamespace(publish=lambda *args,**kwargs:calls.append((args,kwargs)))
    monkeypatch.setattr(configured,'_get_client',lambda:client)
    if kind=='telemetry':configured.republish_telemetry(9,'Synthetic /# + Printer',{'progress':0})
    elif kind=='job':configured.republish_job(9,'Synthetic /# + Printer','completed',{'duration':0})
    elif kind=='alert':configured.republish_alert('maintenance_overdue','warning','Synthetic service','Synthetic details',9,'Synthetic printer')
    else:configured.republish_fleet(0,3,0,0)
    assert len(calls)==1
    args,flags=calls[0];payload=json.loads(args[1])
    suffix={'telemetry':'synthetic______printer/status','job':'synthetic______printer/job','alert':'alerts','fleet':'fleet'}[kind]
    assert args[0]=='synthetic/'+suffix
    assert flags=={'qos':0,'retain':False}
    assert isinstance(payload['timestamp'],(float,int))
    if kind=='telemetry':assert payload['progress']==0 and payload['printer_id']==9
    if kind=='job':assert payload['event']=='completed' and payload['duration']==0
    if kind=='alert':assert payload['type']=='maintenance_overdue' and payload['message']=='Synthetic details'
    if kind=='fleet':assert payload['online']==0 and payload['total']==3

@pytest.mark.parametrize('problem',['disabled','missing_host','invalid_port'])
def test_invalid_or_disabled_config_does_not_open_mqtt_client(configured,review_db,monkeypatch,problem):
    changes={'disabled':('mqtt_republish_enabled','false'),'missing_host':('mqtt_republish_host',''),'invalid_port':('mqtt_republish_port','not-a-port')}
    key,value=changes[problem];review_db.execute(text('UPDATE system_config SET value=:value WHERE key=:key'),{'key':key,'value':value});review_db.commit()
    assert configured._get_client() is None


def test_configuration_invalidation_stops_and_disconnects_previous_client(configured,review_db,monkeypatch):
    calls=[];monkeypatch.setattr(configured,'_client',SimpleNamespace(loop_stop=lambda:calls.append('stop'),disconnect=lambda:calls.append('disconnect')))
    assert configured._get_config()['host']=='synthetic.invalid'
    review_db.execute(text("UPDATE system_config SET value='changed.invalid' WHERE key='mqtt_republish_host'"));review_db.commit()
    configured.invalidate_cache()
    assert calls==['stop','disconnect'] and configured._client is None
    assert configured._get_config()['host']=='changed.invalid'

@pytest.mark.parametrize('event,suffix',[
    ('printer.state_changed','printer_9/status'),('printer.connected','printer_9/status'),
    # Job events carry no printer_name; the topic uses printers.name (P1S-09), the name
    # monitors publish status under (F14-14.1-02), not a printer_<id> fallback.
    ('printer.disconnected','printer_9/status'),('job.started','p1s-09/job'),
    ('job.completed','p1s-09/job'),('job.failed','p1s-09/job'),
    ('notifications.alert_dispatched','alerts'),
])
def test_registered_production_event_bus_handlers_publish_once(configured,monkeypatch,event,suffix):
    from core.event_bus import InMemoryEventBus
    from core.interfaces.event_bus import Event
    calls=[];monkeypatch.setattr(configured,'_get_client',lambda:SimpleNamespace(publish=lambda *a,**k:calls.append((a,k))))
    bus=InMemoryEventBus();configured.register_subscribers(bus);configured.register_subscribers(bus)
    bus.publish(Event(event_type=event,source_module='synthetic',data={'printer_id':9,'alert_type':'maintenance_overdue','title':'Synthetic maintenance'}))
    assert len(calls)==1
    assert calls[0][0][0]=='synthetic/'+suffix
    payload=json.loads(calls[0][0][1]);assert payload['printer_id']==9
    if event.startswith('job.'):assert payload['event']==event.split('.')[-1]
    if event.startswith('notifications.'):assert payload['type']=='maintenance_overdue'


def test_printer_event_without_identity_does_not_publish(configured,monkeypatch):
    from core.event_bus import InMemoryEventBus
    from core.interfaces.event_bus import Event
    calls=[];monkeypatch.setattr(configured,'_get_client',lambda:calls.append('client'))
    bus=InMemoryEventBus();configured.register_subscribers(bus)
    bus.publish(Event('printer.state_changed','synthetic',{'progress':50}))
    assert calls==[]

@pytest.mark.parametrize('outcome',['sent','timeout','connect_failure','publish_failure','connect_rc','publish_rc','wait_failure','stop_failure','disconnect_failure','guard_failure'])
def test_diagnostic_truth_and_cleanup_use_actual_paho_message_state(republisher,monkeypatch,outcome):
    import paho.mqtt.client as mqtt
    from core import itar
    calls=[]
    info=mqtt.MQTTMessageInfo(1)
    if outcome in ['sent','stop_failure','disconnect_failure']:info._set_as_published()
    if outcome=='publish_rc':info.rc=mqtt.MQTT_ERR_NO_CONN
    wait=mqtt.MQTTMessageInfo.wait_for_publish
    monkeypatch.setattr(mqtt.MQTTMessageInfo,'wait_for_publish',lambda self,timeout:(_ for _ in ()).throw(RuntimeError('synthetic-noncredential')) if outcome=='wait_failure' else wait(self,timeout=0.001))
    class Client:
        def connect(self,*a,**k):
            calls.append('connect')
            if outcome=='connect_failure':raise RuntimeError('Synthetic failure')
            return mqtt.MQTT_ERR_NO_CONN if outcome=='connect_rc' else 0
        def loop_start(self):
            calls.append('start')
            self.on_connect(self,None,None,mqtt.ReasonCode(mqtt.PacketTypes.CONNACK,identifier=0))
        def publish(self,*a,**k):
            calls.append('publish')
            if outcome=='publish_failure':raise RuntimeError('Synthetic failure')
            return info
        def loop_stop(self):
            calls.append('stop')
            if outcome=='stop_failure':raise RuntimeError('synthetic-noncredential')
        def disconnect(self):
            calls.append('disconnect')
            if outcome=='disconnect_failure':raise RuntimeError('synthetic-noncredential')
    monkeypatch.setattr(mqtt,'Client',lambda *a,**k:Client())
    monkeypatch.setattr(itar,'enforce_host_destination',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('synthetic-noncredential')) if outcome=='guard_failure' else None)
    response=republisher.test_connection('synthetic.invalid',1883)
    assert response['success'] is (outcome in ['sent','stop_failure','disconnect_failure'])
    assert 'synthetic-noncredential' not in response['message']
    assert calls[-2:]==['stop','disconnect']

@pytest.mark.parametrize('state',['connected','stale','check_failure'])
def test_cached_client_reuse_and_replacement_close_previous_loop(configured,monkeypatch,state,caplog):
    import paho.mqtt.client as mqtt
    from core import itar
    import logging
    caplog.set_level(logging.DEBUG,logger='mqtt_republish')
    calls=[]
    class OldClient:
        def is_connected(self):
            if state=='check_failure':raise RuntimeError('synthetic-noncredential')
            return state=='connected'
        def loop_stop(self):calls.append('old-stop')
        def disconnect(self):calls.append('old-disconnect')
    old=OldClient();monkeypatch.setattr(configured,'_client',old)
    class NewClient:
        def connect(self,*a,**k):calls.append('new-connect');return 0
        def loop_start(self):calls.append('new-start');return 0
    new=NewClient();monkeypatch.setattr(mqtt,'Client',lambda *a,**k:new)
    monkeypatch.setattr(itar,'enforce_host_destination',lambda *a,**k:None)
    result=configured._get_client()
    if state=='connected':assert result is old and calls==[]
    else:
        assert result is new
        assert calls==['old-stop','old-disconnect','new-connect','new-start']
    assert 'synthetic-noncredential' not in caplog.text

@pytest.mark.parametrize('failure',['connect_rc','connect_exception','loop_start','loop_start_rc'])
def test_partial_initialization_is_cleaned_and_never_cached(configured,monkeypatch,failure):
    import paho.mqtt.client as mqtt
    from core import itar
    calls=[]
    class Client:
        def connect(self,*a,**k):
            if failure=='connect_exception':raise RuntimeError('synthetic-noncredential')
            return mqtt.MQTT_ERR_NO_CONN if failure=='connect_rc' else 0
        def loop_start(self):
            if failure=='loop_start':raise RuntimeError('synthetic-noncredential')
            return mqtt.MQTT_ERR_INVAL if failure=='loop_start_rc' else 0
        def loop_stop(self):calls.append('stop')
        def disconnect(self):calls.append('disconnect')
    monkeypatch.setattr(mqtt,'Client',lambda *a,**k:Client())
    monkeypatch.setattr(itar,'enforce_host_destination',lambda *a,**k:None)
    assert configured._get_client() is None and configured._client is None
    assert calls==['stop','disconnect']

@pytest.mark.parametrize('operation',['disconnect','invalidate_cache'])
def test_cleanup_failure_cannot_skip_disconnect_or_leave_cached_client(configured,monkeypatch,operation):
    calls=[]
    class Client:
        def loop_stop(self):calls.append('stop');raise RuntimeError('Synthetic cleanup')
        def disconnect(self):calls.append('disconnect')
    monkeypatch.setattr(configured,'_client',Client())
    getattr(configured,operation)()
    assert calls==['stop','disconnect'] and configured._client is None

def test_simultaneous_callers_select_only_one_connected_replacement(configured,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import paho.mqtt.client as mqtt
    from core import itar
    calls=[];barrier=threading.Barrier(4)
    class Client:
        def is_connected(self):return True
        def connect(self,*a,**k):calls.append('connect');return 0
        def loop_start(self):calls.append('start');return 0
    selected=Client();monkeypatch.setattr(mqtt,'Client',lambda *a,**k:selected)
    monkeypatch.setattr(itar,'enforce_host_destination',lambda *a,**k:None)
    # Warm configuration before concurrent callers exercise client selection.
    assert configured._get_config()
    def get():barrier.wait(timeout=2);return configured._get_client()
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:get(),range(4)))
    assert all(value is selected for value in results)
    assert calls==['connect','start'] and configured._client is selected


def _count_connects(module, monkeypatch):
    calls = []
    real = module.engine.connect
    def connect(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(module.engine, "connect", connect)
    return calls


def test_disabled_republish_config_is_cached_like_an_enabled_one(republisher, monkeypatch):
    # Disabled (no mqtt_republish_enabled row): one read within the TTL, not one per event.
    calls = _count_connects(republisher, monkeypatch)
    assert republisher._get_config() is None and republisher._get_config() is None
    assert len(calls) == 1
    republisher.invalidate_cache()
    assert republisher._get_config() is None and len(calls) == 2


def test_disabled_republish_skips_alert_scope_and_job_lookups(republisher, monkeypatch):
    from modules.notifications import recipient_policy
    def refuse(*args, **kwargs):
        raise AssertionError("republish disabled: no alert scope or quiet-hours work")
    monkeypatch.setattr(recipient_policy, "resolve_alert_scope", refuse)
    monkeypatch.setattr(republisher, "_printer_topic_name", refuse)
    published = []
    monkeypatch.setattr(republisher, "_publish", lambda *args: published.append(args))
    republisher._on_alert_dispatched(SimpleNamespace(data={"alert_type": "job_submitted", "job_id": 1}))
    republisher._on_job_event(SimpleNamespace(event_type="job.completed", data={"printer_id": 9}))
    assert published == []


def test_client_awaiting_connack_is_not_retired(configured, monkeypatch):
    import time
    calls = []
    class Connecting:
        def is_connected(self): return False
        def loop_stop(self): calls.append('stop')
        def disconnect(self): calls.append('disconnect')
    client = Connecting()
    monkeypatch.setattr(configured, '_client', client)
    monkeypatch.setattr(configured, '_client_started', time.monotonic())
    assert configured._get_client() is client and calls == []
    monkeypatch.setattr(configured, '_client_started', time.monotonic() - configured._CONNECT_GRACE_SECONDS - 1)
    import paho.mqtt.client as mqtt
    from core import itar
    monkeypatch.setattr(mqtt, 'Client', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic-noncredential')))
    monkeypatch.setattr(itar, 'enforce_host_destination', lambda *a, **k: None)
    assert configured._get_client() is None and calls == ['stop', 'disconnect']
