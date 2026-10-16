import json
import time
from dataclasses import replace
from types import SimpleNamespace
import pytest


@pytest.fixture(autouse=True)
def _loopback_device_peers(monkeypatch):
    # Brokers/peers in this file run on loopback, which the production device
    # destination policy blocks; exempt exactly 127.0.0.1 and ::1 here (also in
    # spawned workers). test_cert_A_destination_policy covers the policy.
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
from modules.printers.telemetry.bambu.ams_acquisition_session import (
    AMSSessionController, _run_owned_worker, read_selected_ams)
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact

CONFIG=BambuAdapterConfig('synthetic','synthetic','127.0.0.1','')
KEY='a'*64
INC='b'*32


class Client:
    def __init__(self, controller, race=False, publish_rc=0):
        self.controller=controller;self.race=race;self.publish_rc=publish_rc;self.published=[]
    def subscribe(self,topic,qos):
        if self.race:self.controller.on_subscribe(self,None,7,[0])
        return (0,7)
    def publish(self,topic,body,**kwargs):
        self.published.append((topic,json.loads(body),kwargs))
        return SimpleNamespace(rc=self.publish_rc)


def controller(race=False,publish_rc=0):
    c=AMSSessionController(CONFIG,KEY,1,[1],INC);client=Client(c,race,publish_rc)
    c.on_connect(client,None,{},0)
    return c,client


def message(c,client,packet=None,**kw):
    raw={'print':packet if packet is not None else {'ams':{'ams':[{'id':0,'tray':[{'id':0,'tray_type':'PETG'}]}]}}}
    c.on_message(client,None,SimpleNamespace(payload=json.dumps(raw).encode(),
        topic=kw.get('topic',CONFIG.topic_report),retain=kw.get('retain',False)))


def arm(c,client):c.on_subscribe(client,None,7,[SimpleNamespace(value=0)])


def test_suback_race_exact_read_request_and_raw_full_report():
    c,client=controller(race=True)
    c.on_subscribe(client,None,7,[0])
    assert len(client.published)==1
    assert client.published[0][1]=={'pushing':{'sequence_id':'0','command':'pushall'}}
    message(c,client,{'gcode_state':'IDLE'})
    assert not c.done.is_set()
    message(c,client)
    snapshot=c.freeze()
    assert snapshot.incarnation==INC and snapshot.trays[0].material=='PETG'
    c.on_disconnect(client,None)
    assert snapshot.trays[0].material=='PETG'


def test_pre_ack_packets_and_wrong_mid_cannot_arm():
    c,client=controller()
    message(c,client)
    c.on_subscribe(client,None,8,[0])
    assert not client.published and not c.done.is_set()
    arm(c,client);message(c,client)
    assert c.freeze() is not None


@pytest.mark.parametrize('change',[{'retain':True},{'retain':None},{'retain':0},{'topic':'device/foreign/report'}])
def test_packet_provenance_refusal(change):
    c,client=controller();arm(c,client);message(c,client,**change)
    assert c.done.is_set() and c.freeze() is None


@pytest.mark.parametrize('action',['disconnect','reconnect','invalid','publish_failure'])
def test_valid_data_cannot_survive_invalidation_before_freeze(action):
    c,client=controller(publish_rc=1 if action=='publish_failure' else 0)
    arm(c,client);message(c,client)
    if action=='disconnect':c.on_disconnect(client,None)
    if action=='reconnect':c.on_connect(client,None,{},0)
    if action=='invalid':message(c,client,{'ams':{'ams':[{'id':0,'tray':[{'id':0}]}]}})
    assert c.freeze() is None


def _snapshot(incarnation,key,owner):
    return RawAMSSnapshot(incarnation,'c'*32,1,time.monotonic(),key,owner,
        (RawTrayFact(1,0,0,'PETG','reported'),))


def worker_good(channel,config,key,owner,slots,incarnation,deadline):
    channel.put({'snapshot':_snapshot(incarnation,key,owner),'error':None})


def worker_hung_cleanup(channel,config,key,owner,slots,incarnation,deadline):
    channel.put({'snapshot':_snapshot(incarnation,key,owner),'error':None})
    while True:time.sleep(.1)


def worker_crash(channel,config,key,owner,slots,incarnation,deadline):
    import os
    os._exit(7)


def worker_foreign(channel,config,key,owner,slots,incarnation,deadline):
    channel.put({'snapshot':_snapshot('d'*32,key,2),'error':None})


def worker_bad_material(channel,config,key,owner,slots,incarnation,deadline):
    value=replace(_snapshot(incarnation,key,owner),trays=(RawTrayFact(1,0,0,None,'reported'),))
    channel.put({'snapshot':value,'error':None})


def worker_old_receipt(channel,config,key,owner,slots,incarnation,deadline):
    channel.put({'snapshot':replace(_snapshot(incarnation,key,owner),received_monotonic=0),'error':None})


def test_native_owned_worker_cleanup_and_binding():
    result=_run_owned_worker(CONFIG,KEY,1,[1],3,worker=worker_good)
    assert result.success and result.cleanup_confirmed and result.snapshot is not None


@pytest.mark.parametrize('worker',[worker_hung_cleanup,worker_crash,worker_foreign,worker_old_receipt,worker_bad_material])
def test_native_result_or_eof_does_not_replace_cleanup_or_authority(worker):
    import multiprocessing
    before={p.pid for p in multiprocessing.active_children()}
    result=_run_owned_worker(CONFIG,KEY,1,[1],1,worker=worker)
    assert not result.success and result.snapshot is None and result.cleanup_confirmed
    assert {p.pid for p in multiprocessing.active_children()}==before


def test_invalid_public_configuration_never_spawns():
    assert not read_selected_ams(CONFIG,configuration_key='bad',owner_org_id=1,selected_slots=[1]).success
    assert not read_selected_ams(CONFIG,configuration_key=KEY,owner_org_id=1,selected_slots=[1],timeout=True).success


def test_every_cleanup_step_is_attempted_after_failure():
    from modules.printers.telemetry.bambu.ams_acquisition_session import _cleanup_owned
    calls=[]
    class Process:
        pid=123;exitcode=None
        def is_alive(self):calls.append('alive');raise RuntimeError('private')
        def terminate(self):calls.append('terminate');raise RuntimeError('private')
        def join(self,timeout):calls.append('join');raise RuntimeError('private')
        def kill(self):calls.append('kill');raise RuntimeError('private')
        def close(self):calls.append('process-close');raise RuntimeError('private')
    class Channel:
        def close(self):calls.append('queue-close');raise RuntimeError('private')
        def join_thread(self):calls.append('queue-join');raise RuntimeError('private')
    assert _cleanup_owned(Process(),Channel()) is False
    assert {'alive','terminate','join','kill','process-close','queue-close','queue-join'}<=set(calls)


def test_early_result_late_normal_exit_cannot_succeed(monkeypatch):
    import modules.printers.telemetry.bambu.ams_acquisition_session as module
    clock=iter([10,10.1,10.2,10.3,11.1])
    monkeypatch.setattr(module.time,'monotonic',lambda:next(clock))
    class Channel:
        def get(self,timeout):
            return {'snapshot':RawAMSSnapshot(INC,'c'*32,1,10.25,KEY,1,
                (RawTrayFact(1,0,0,'PETG','reported'),)),'error':None}
        def close(self):pass
        def join_thread(self):pass
    class Process:
        pid=1;exitcode=0
        def start(self):pass
        def join(self,timeout):pass
        def is_alive(self):return False
        def close(self):pass
    class Context:
        def Queue(self,**kwargs):return Channel()
        def Process(self,**kwargs):return Process()
    monkeypatch.setattr(module.multiprocessing,'get_context',lambda mode:Context())
    monkeypatch.setattr(module.uuid,'uuid4',lambda:SimpleNamespace(hex=INC))
    result=module._run_owned_worker(CONFIG,KEY,1,[1],1)
    assert not result.success and result.snapshot is None and result.cleanup_confirmed


def test_host_guard_runs_before_any_network(monkeypatch):
    from modules.printers.telemetry.bambu import ams_acquisition_session as module
    from modules.printers.telemetry.bambu import adapter
    from core import itar
    observed=[]
    monkeypatch.setattr(itar,'enforce_host_destination',lambda *a,**k:(_ for _ in ()).throw(ValueError('private')))
    monkeypatch.setattr(adapter.BambuTelemetryAdapter,'start',lambda self:pytest.fail('Host denial touched network'))
    import logging
    # _network_worker is a child-process entry point and disables logging
    # process-wide; running it in-process must not silence later tests.
    previous=logging.root.manager.disable
    try:
        module._network_worker(SimpleNamespace(put=observed.append),CONFIG,KEY,1,[1],INC,time.monotonic()+1)
    finally:
        logging.disable(previous)
    assert observed==[{'snapshot':None,'error':'AMS acquisition unavailable'}]


# The established owned TLS fixture replaces only endpoint resolution in the
# child. Credentials, TLS, callback controller, process boundary and cleanup
# are the actual acquisition implementation.
from tests.test_contracts.test_education_connected_print_workflow import tls_mqtt, SYNTHETIC_ACCESS_CODE


def worker_owned_tls_endpoint(channel,config,key,owner,slots,incarnation,deadline):
    from modules.printers.telemetry.bambu import broker_policy
    from modules.printers.telemetry.bambu.ams_acquisition_session import _network_worker
    broker_policy.resolve_bambu_broker_config=lambda *a,**k:broker_policy.BrokerEndpoint(config.host,config.port,True)
    _network_worker(channel,config,key,owner,slots,incarnation,deadline)


@pytest.mark.parametrize('retained_seed',[False,True])
def test_actual_owned_tls_broker_report_and_retained_refusal(tls_mqtt,retained_seed):
    import ssl
    from threading import Event
    import paho.mqtt.client as mqtt
    port,commands=tls_mqtt
    ready=Event()
    peer=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    peer.username_pw_set('bblp',SYNTHETIC_ACCESS_CODE)
    peer.tls_set(cert_reqs=ssl.CERT_NONE);peer.tls_insecure_set(True)
    topic='device/synthetic-printer/report'
    report=json.dumps({'print':{'command':'push_status','ams':{'ams':[
        {'id':0,'tray':[{'id':0,'tray_type':'PETG'}]}]}}})
    peer.on_connect=lambda c,*a:c.subscribe('device/synthetic-printer/request')
    peer.on_subscribe=lambda *a:ready.set()
    peer.on_message=lambda c,u,m:c.publish(topic,report,qos=0,retain=False)
    try:
        peer.connect('127.0.0.1',port,keepalive=5);peer.loop_start()
        assert ready.wait(3)
        if retained_seed:
            sent=peer.publish(topic,report,qos=1,retain=True);sent.wait_for_publish(timeout=3)
            assert sent.is_published()
        config=BambuAdapterConfig('owned-tls','synthetic-printer','127.0.0.1',SYNTHETIC_ACCESS_CODE,port=port)
        result=_run_owned_worker(config,KEY,1,[1],5,worker=worker_owned_tls_endpoint)
        assert result.cleanup_confirmed
        assert result.success is (not retained_seed)
        if result.success:assert result.snapshot.trays[0].material=='PETG'
        else:assert result.snapshot is None
        assert commands and all(cmd=={'pushing':{'sequence_id':'0','command':'pushall'}} for cmd in commands)
    finally:
        peer.disconnect();peer.loop_stop()


@pytest.mark.parametrize('value', [0, 1, None, 'true'])
def test_parent_rejects_non_boolean_idle_evidence(value):
    from modules.printers.telemetry.bambu.ams_acquisition_session import _valid_snapshot
    captured = _snapshot(INC, KEY, 1)
    changed = replace(captured, idle_without_error=value)
    now = time.monotonic()
    assert not _valid_snapshot(changed, key=KEY, owner=1, slots=[1], incarnation=INC,
                               started=now-1, deadline=now+1, now=now)
