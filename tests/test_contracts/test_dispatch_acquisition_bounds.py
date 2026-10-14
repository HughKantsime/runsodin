"""Owned spawn workers, never another process or physical device."""
import multiprocessing
import time
from types import SimpleNamespace
import pytest
from modules.printers import dispatch_acquisition as module
from tests.test_contracts.test_dispatch_http_readiness_behavior import peer


def _hang(sender,creds,deadline):
    try: time.sleep(30)
    finally: sender.close()


def _result_then_hang(sender,creds,deadline):
    sender.send(True)
    time.sleep(30)


def _malformed(sender,creds,deadline):
    sender.send({'ready':True});sender.close()


def _normal(sender,creds,deadline):
    sender.send(True);sender.close()


def _exit_without_result(sender,creds,deadline):
    sender.close()


@pytest.fixture
def owned_worker(monkeypatch):
    context = multiprocessing.get_context('spawn'); processes = []; snapshots = []
    def select(target):
        def create(*,target: object,args,daemon):
            snapshots.append(args[1])
            proc = context.Process(target=selected[0],args=args,daemon=daemon)
            processes.append(proc)
            return proc
        selected = [target]
        monkeypatch.setattr(module,'_spawn_context',lambda:SimpleNamespace(Pipe=context.Pipe,Process=create))
    yield select,processes,snapshots
    assert all(proc._closed for proc in processes)


@pytest.mark.parametrize('worker', [_hang,_result_then_hang,_malformed,_exit_without_result])
def test_unfinished_or_invalid_worker_refused_and_reaped(owned_worker,worker):
    select,processes,snapshots = owned_worker;select(worker)
    before = {proc.pid for proc in multiprocessing.active_children()}
    start = time.monotonic()
    assert not module.bounded_http_status_is_idle({'api_type':'prusalink','ip':'127.0.0.1','api_key':'synthetic'},start+1)
    assert time.monotonic()-start < 4
    assert {proc.pid for proc in multiprocessing.active_children()} == before


def test_strict_success_requires_normal_exit_and_credentials_whitelist(owned_worker):
    select,processes,snapshots = owned_worker;select(_normal)
    assert module.bounded_http_status_is_idle({'api_type':'prusalink','ip':'127.0.0.1','api_key':'synthetic','unneeded_private_record':'not passed'},time.monotonic()+3)
    assert 'unneeded_private_record' not in snapshots[0]


@pytest.mark.parametrize('deadline', [None,False,0,float('nan'),float('inf'),10**1000])
def test_invalid_deadline_refused_before_spawn(monkeypatch,deadline):
    monkeypatch.setattr(module,'_spawn_context',lambda:pytest.fail('Invalid budget must not spawn'))
    assert not module.bounded_http_status_is_idle({'api_type':'prusalink'},deadline)


def test_actual_spawned_http_worker_reads_owned_peer_and_exits(peer):
    state,creds = peer
    before = {proc.pid for proc in multiprocessing.active_children()}
    assert module.bounded_http_status_is_idle(creds,time.monotonic()+5)
    assert len(state['calls']) == 1
    assert {proc.pid for proc in multiprocessing.active_children()} == before


@pytest.mark.parametrize('failure', ['terminate','join','close'])
def test_cleanup_failures_return_false_and_continue_owned_cleanup(monkeypatch,failure):
    calls = []
    class Pipe:
        def close(self):calls.append('pipe_close')
        def poll(self,timeout):return False
    class Process:
        alive = True
        def start(self):pass
        def is_alive(self):return self.alive
        def terminate(self):
            calls.append('terminate')
            if failure == 'terminate':raise RuntimeError('synthetic private marker')
            self.alive = False
        def join(self,timeout):
            calls.append('join')
            if failure == 'join':raise RuntimeError('synthetic private marker')
        def kill(self):calls.append('kill');self.alive=False
        def close(self):
            calls.append('close')
            if failure == 'close':raise RuntimeError('synthetic private marker')
    proc = Process()
    monkeypatch.setattr(module,'_spawn_context',lambda:SimpleNamespace(Pipe=lambda **kwargs:(Pipe(),Pipe()),Process=lambda **kwargs:proc))
    assert module.bounded_http_status_is_idle({'api_type':'prusalink'},time.monotonic()+1) is False
    assert 'close' in calls
    if failure == 'terminate':assert 'kill' in calls
