"""One-command power preparation through owned peers and owned spawn workers."""
import multiprocessing
import time
from types import SimpleNamespace
import pytest


@pytest.fixture(autouse=True)
def _loopback_plug_peers(monkeypatch):
    # The plug peers in this file listen on 127.0.0.1, which the production
    # device destination policy blocks; exempt exactly that address here
    # (and in spawned workers). test_cert_A_destination_policy covers the policy.
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
from modules.printers import plug_power_acquisition as module
from tests.test_contracts.test_smart_plug_http_peer_behavior import plug_peer


def _hang(sender, config, deadline):
    time.sleep(30)


def _early_then_hang(sender, config, deadline):
    sender.send(('observed', True))
    time.sleep(30)


def _malformed(sender, config, deadline):
    sender.send(('observed', 1));sender.close()


def _normal(sender, config, deadline):
    sender.send(('observed', True));sender.close()


@pytest.fixture
def owned_worker(monkeypatch):
    context=multiprocessing.get_context('spawn'); processes=[]; snapshots=[]
    def select(worker):
        def create(*, target, args, daemon):
            snapshots.append(args[1])
            process=context.Process(target=worker,args=args,daemon=daemon)
            processes.append(process)
            return process
        monkeypatch.setattr(module,'_spawn_context',lambda:SimpleNamespace(Pipe=context.Pipe,Process=create))
    yield select, processes, snapshots
    assert all(process._closed for process in processes)


@pytest.mark.parametrize('worker',[_hang,_early_then_hang,_malformed])
def test_worker_timeout_or_invalid_receipt_is_uncertain_and_reaped(owned_worker,worker):
    select, processes, snapshots=owned_worker;select(worker)
    before={process.pid for process in multiprocessing.active_children()}
    start=time.monotonic()
    attempt=module.bounded_http_power_on({'type':'tasmota','host':'127.0.0.1'},start+1)
    assert attempt==module.PowerAttempt('uncertain')
    assert time.monotonic()-start<4
    assert len(processes)==1
    assert {process.pid for process in multiprocessing.active_children()}==before


def test_normal_receipt_and_only_protocol_inputs(owned_worker):
    select, processes, snapshots=owned_worker;select(_normal)
    attempt=module.bounded_http_power_on({'type':'tasmota','host':'127.0.0.1','private_record':'not sent'},time.monotonic()+3)
    assert attempt==module.PowerAttempt('accepted',True)
    assert snapshots==[{'type':'tasmota','host':'127.0.0.1'}]


@pytest.mark.parametrize('deadline',[None,False,0,float('nan'),float('inf'),10**1000])
def test_bad_deadline_refuses_without_worker(monkeypatch,deadline):
    monkeypatch.setattr(module,'_spawn_context',lambda:pytest.fail('must not spawn'))
    assert module.bounded_http_power_on({'type':'tasmota','host':'127.0.0.1'},deadline)==module.PowerAttempt('refused')


@pytest.mark.parametrize('kind',['tasmota','homeassistant'])
def test_actual_spawned_on_is_once_and_worker_exits(plug_peer,kind,monkeypatch):
    monkeypatch.setenv('ODIN_ITAR_MODE','0')
    state,host=plug_peer
    config={'type':kind,'host':host if kind=='tasmota' else 'http://'+host,'entity_id':'switch.synthetic','auth_token':'synthetic-token'}
    before={process.pid for process in multiprocessing.active_children()}
    assert module.bounded_http_power_on(config,time.monotonic()+5)==module.PowerAttempt('accepted',True)
    assert [call[0] for call in state['source']]==(['GET'] if kind=='tasmota' else ['POST','GET'])
    assert state['sink']==[]
    assert {process.pid for process in multiprocessing.active_children()}==before


def test_actual_off_observation_refuses_admission(plug_peer,monkeypatch):
    monkeypatch.setenv('ODIN_ITAR_MODE','0')
    state,host=plug_peer;state['body']=b'{"POWER":"OFF"}'
    assert module.bounded_http_power_on({'type':'tasmota','host':host},time.monotonic()+5)==module.PowerAttempt('refused',False)
    assert len(state['source'])==1


def test_actual_redirect_is_uncertain_without_resend(plug_peer,monkeypatch):
    monkeypatch.setenv('ODIN_ITAR_MODE','0')
    state,host=plug_peer;state['redirect']=True
    assert module.bounded_http_power_on({'type':'tasmota','host':host},time.monotonic()+5)==module.PowerAttempt('uncertain')
    assert len(state['source'])==1
    assert state['sink']==[]


@pytest.mark.parametrize('kind',['tasmota','homeassistant'])
def test_actual_http_hang_is_uncertain_reaped_and_not_retried(plug_peer,monkeypatch,kind):
    import threading
    monkeypatch.setenv('ODIN_ITAR_MODE','0')
    state,host=plug_peer;gate=threading.Event();state['response_gate']=gate
    config={'type':kind,'host':host if kind=='tasmota' else 'http://'+host,'entity_id':'switch.synthetic','auth_token':'synthetic-token'}
    before={process.pid for process in multiprocessing.active_children()}
    started=time.monotonic()
    try:
        assert module.bounded_http_power_on(config,started+1.5)==module.PowerAttempt('uncertain')
        assert time.monotonic()-started<4.5
        assert len(state['source'])==1
        assert state['sink']==[]
        assert {process.pid for process in multiprocessing.active_children()}==before
    finally:
        gate.set()


def _normal_off(sender, config, deadline):
    assert config == {'type': 'tasmota', 'host': '192.0.2.1', '_action': 'OFF'}
    sender.send(('observed', False))
    sender.close()


def test_owned_explicit_off_accepts_false_only_after_normal_exit_and_cleanup(owned_worker):
    select, processes, snapshots = owned_worker
    select(_normal_off)
    receipt = module.bounded_http_power_action({'type': 'tasmota', 'host': '192.0.2.1'}, time.monotonic() + 5, 'OFF')
    assert receipt.confirmed(False)
    assert not receipt.confirmed(True)
    assert receipt.attempt.normal_exit and receipt.attempt.reaped and receipt.attempt.ipc_closed
    assert not receipt.attempt.start_uncertain
    assert len(processes) == 1
