"""Real config files and locks; supervisor boundary substituted, no camera/device."""
from types import SimpleNamespace
import os
import pytest


def test_config_changed_during_cooldown_is_eventually_applied(tmp_path,monkeypatch):
    import yaml,subprocess
    from modules.printers import route_utils
    config_path=tmp_path/'go2rtc.yaml';lock_path=tmp_path/'go2rtc.lock'
    clock=[1000.0]
    config_a={'streams':{'synthetic':'synthetic-a'}}
    config_b={'streams':{'synthetic':'synthetic-b'}}
    config_path.write_text(yaml.safe_dump(config_a));os.utime(config_path,(1000,1000))
    monkeypatch.setattr(route_utils,'GO2RTC_CONFIG',str(config_path))
    monkeypatch.setattr(route_utils,'_GO2RTC_LOCKFILE',str(lock_path))
    monkeypatch.setattr(route_utils,'time',SimpleNamespace(time=lambda:clock[0]))
    monkeypatch.setattr(route_utils,'_GO2RTC_MIN_RESTART_INTERVAL',30)
    applied=[config_a]
    def supervisor(command,**kwargs):
        assert command==['supervisorctl','restart','go2rtc']
        applied.append(yaml.safe_load(config_path.read_text()))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess,'run',supervisor)
    clock[0]=1005
    route_utils._go2rtc_restart_with_lock(config_b)
    clock[0]=1100
    route_utils._go2rtc_restart_with_lock(config_b)
    assert applied[-1]==config_b


@pytest.fixture
def deferred_sync(tmp_path,monkeypatch):
    import subprocess,threading,time,yaml
    from modules.printers import route_utils as module
    path=tmp_path/'config.yaml'
    monkeypatch.setattr(module,'GO2RTC_CONFIG',str(path))
    monkeypatch.setattr(module,'_GO2RTC_LOCKFILE',str(tmp_path/'sync.lock'))
    monkeypatch.setattr(module,'_GO2RTC_MIN_RESTART_INTERVAL',0.08)
    calls=[];event=threading.Event();outcomes=[]
    def supervisor(command,**kwargs):
        assert command==['supervisorctl','restart','go2rtc']
        assert kwargs['timeout']==5
        calls.append(yaml.safe_load(path.read_text()))
        outcome=outcomes.pop(0) if outcomes else 0
        if isinstance(outcome,Exception):raise outcome
        if outcome==0:event.set()
        return SimpleNamespace(returncode=outcome)
    monkeypatch.setattr(subprocess,'run',supervisor)
    yield module,path,calls,event,outcomes
    # Do not let synthetic retry workers survive fixture restoration.
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        with module._go2rtc_retry_lock:
            timer=module._go2rtc_retry_timers.pop(str(path),None)
            workers=list(module._go2rtc_retry_workers.get(str(path),set()))
        if timer:timer.cancel();timer.join(timeout=1)
        for worker in workers:worker.join(timeout=1)
        if not timer and not workers:break
    with module._go2rtc_retry_lock:
        assert not module._go2rtc_retry_workers.get(str(path))
        assert str(path) not in module._go2rtc_retry_timers
        module._go2rtc_pending_configs.pop(str(path),None)


def test_one_change_eventually_applies_without_second_caller(deferred_sync):
    module,path,calls,event,_=deferred_sync
    module._go2rtc_restart_with_lock({'streams':{'synthetic':'a'}})
    event.clear()
    module._go2rtc_restart_with_lock({'streams':{'synthetic':'b'}})
    assert len(calls)==1
    assert event.wait(2),'Pending config must apply without another request'
    assert calls[-1]=={'streams':{'synthetic':'b'}}
    assert len(calls)==2


def test_pending_restart_uses_latest_config(deferred_sync):
    module,path,calls,event,_=deferred_sync
    module._go2rtc_restart_with_lock({'streams':{'synthetic':'a'}})
    event.clear()
    module._go2rtc_restart_with_lock({'streams':{'synthetic':'b'}})
    module._go2rtc_restart_with_lock({'streams':{'synthetic':'c'}})
    assert event.wait(2)
    assert calls==[{'streams':{'synthetic':'a'}},{'streams':{'synthetic':'c'}}]


@pytest.mark.parametrize('failure',['nonzero','timeout'])
def test_restart_failure_remains_pending_and_recovers(deferred_sync,failure):
    import subprocess,json,hashlib,yaml
    module,path,calls,event,outcomes=deferred_sync
    outcomes.append(1 if failure=='nonzero' else subprocess.TimeoutExpired('synthetic',5))
    config={'streams':{'synthetic':'a'}}
    module._go2rtc_restart_with_lock(config)
    state=json.loads(path.with_name(path.name+'.sync-state.json').read_text())
    assert 'applied' not in state
    assert event.wait(2)
    assert calls==[config,config]
    state=json.loads(path.with_name(path.name+'.sync-state.json').read_text())
    # Successful subprocess returns before its fingerprint is written.
    import time
    until=time.monotonic()+2
    while 'applied' not in state and time.monotonic()<until:
        time.sleep(0.01);state=json.loads(path.with_name(path.name+'.sync-state.json').read_text())
    assert state['applied']==hashlib.sha256(yaml.safe_dump(config,sort_keys=True).encode()).hexdigest()


def test_config_and_state_are_private_and_state_has_no_stream_values(deferred_sync):
    import json,stat
    module,path,calls,event,_=deferred_sync
    module._go2rtc_restart_with_lock({'streams':{'synthetic':'synthetic-private-url'}})
    state=path.with_name(path.name+'.sync-state.json')
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    assert stat.S_IMODE(state.stat().st_mode)==0o600
    assert 'synthetic-private-url' not in state.read_text()
    assert set(json.loads(state.read_text()))=={'last_attempt','applied','intent_revision'}


def test_unchanged_successful_config_does_not_restart(deferred_sync):
    module,path,calls,event,_=deferred_sync
    config={'streams':{'synthetic':'a'}}
    module._go2rtc_restart_with_lock(config)
    module._go2rtc_restart_with_lock(config)
    assert calls==[config]
    module._go2rtc_restart_with_lock(config,force=True)
    assert calls==[config,config]


def test_competing_processes_apply_pending_config_once(tmp_path,monkeypatch):
    import multiprocessing,subprocess,time,yaml,json
    from modules.printers import route_utils as module
    context=multiprocessing.get_context('fork')
    path=tmp_path/'config.yaml';receipts=tmp_path/'restarts.jsonl'
    monkeypatch.setattr(module,'GO2RTC_CONFIG',str(path))
    monkeypatch.setattr(module,'_GO2RTC_LOCKFILE',str(tmp_path/'sync.lock'))
    monkeypatch.setattr(module,'_GO2RTC_MIN_RESTART_INTERVAL',0.3)
    def supervisor(*args,**kwargs):
        with receipts.open('a') as stream:
            stream.write(json.dumps(yaml.safe_load(path.read_text()))+'\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess,'run',supervisor)
    module._go2rtc_restart_with_lock({'streams':{'synthetic':'a'}})
    def worker():
        module._go2rtc_restart_with_lock({'streams':{'synthetic':'b'}})
        time.sleep(0.8) # Allow each real deferred worker to run under shared flock.
    processes=[context.Process(target=worker) for _ in range(4)]
    try:
        for process in processes:process.start()
        for process in processes:
            process.join(timeout=3)
            assert process.exitcode==0
        assert [json.loads(line) for line in receipts.read_text().splitlines()]==[
            {'streams':{'synthetic':'a'}},{'streams':{'synthetic':'b'}}]
    finally:
        for process in processes:
            if process.is_alive():process.terminate();process.join(timeout=1)


@pytest.mark.parametrize('timestamp',[float('inf'),float('nan'),True,'invalid',10**20])
def test_corrupt_or_future_attempt_time_cannot_strand_sync(deferred_sync,timestamp):
    import json
    module,path,calls,event,_=deferred_sync
    state=path.with_name(path.name+'.sync-state.json')
    state.write_text(json.dumps({'last_attempt':timestamp}))
    module._go2rtc_restart_with_lock({'streams':{'synthetic':'a'}})
    assert event.wait(2)
    assert calls==[{'streams':{'synthetic':'a'}}]


@pytest.mark.parametrize('failed_file',['config','state'])
def test_transient_write_failure_recovers_without_second_caller(deferred_sync,monkeypatch,failed_file):
    module,path,calls,event,_=deferred_sync
    original=module._go2rtc_atomic_write
    failed=[False]
    def fail_once(target,content):
        matches=(target==str(path)) if failed_file=='config' else target.endswith('.sync-state.json')
        if matches and not failed[0]:
            failed[0]=True;raise OSError('Synthetic transient write failure')
        return original(target,content)
    monkeypatch.setattr(module,'_go2rtc_atomic_write',fail_once)
    module._go2rtc_restart_with_lock({'streams':{'synthetic':'a'}})
    assert calls==[]
    assert event.wait(2)
    assert calls==[{'streams':{'synthetic':'a'}}]


def test_unchanged_legacy_config_is_made_private(deferred_sync):
    import yaml,stat
    module,path,calls,event,_=deferred_sync
    config={'streams':{'synthetic':'a'}}
    path.write_text(yaml.safe_dump(config));path.chmod(0o644)
    module._go2rtc_restart_with_lock(config)
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    assert calls==[config]


@pytest.mark.parametrize('failed_file',['config','state','state_after_config','state_after_restart'])
@pytest.mark.parametrize('newer',['distinct','unchanged','aba'])
def test_failed_worker_cannot_overwrite_newer_process_config(tmp_path,monkeypatch,failed_file,newer):
    import multiprocessing,subprocess,time,yaml,json
    from modules.printers import route_utils as module
    context=multiprocessing.get_context('fork');ready=context.Event()
    path=tmp_path/'config.yaml';receipts=tmp_path/'restarts.jsonl'
    monkeypatch.setattr(module,'GO2RTC_CONFIG',str(path))
    monkeypatch.setattr(module,'_GO2RTC_LOCKFILE',str(tmp_path/'sync.lock'))
    monkeypatch.setattr(module,'_GO2RTC_MIN_RESTART_INTERVAL',0.3)
    def supervisor(*args,**kwargs):
        with receipts.open('a') as stream:stream.write(json.dumps(yaml.safe_load(path.read_text()))+'\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess,'run',supervisor)
    initial={'streams':{'synthetic':'initial'}};stale={'streams':{'synthetic':'stale'}};latest={'streams':{'synthetic':'latest'}}
    if newer in ('unchanged','aba'):latest=initial
    module._go2rtc_restart_with_lock(initial)
    def worker():
        original=module._go2rtc_atomic_write;failed=False;state_writes=0
        def fail_once(target,content):
            nonlocal failed,state_writes
            if target.endswith('.sync-state.json'):state_writes+=1
            failure_number={'state':1,'state_after_config':2,'state_after_restart':3}.get(failed_file)
            matches=(target==str(path)) if failed_file=='config' else target.endswith('.sync-state.json') and state_writes==failure_number
            if matches and not failed:failed=True;raise OSError('Synthetic write failure')
            return original(target,content)
        module._go2rtc_atomic_write=fail_once
        module._go2rtc_restart_with_lock(stale,force=True)
        assert failed
        ready.set();time.sleep(0.9)
    process=context.Process(target=worker)
    try:
        process.start();assert ready.wait(2)
        if newer=='aba':module._go2rtc_restart_with_lock({'streams':{'synthetic':'intermediate'}},force=True)
        module._go2rtc_restart_with_lock(latest,force=True)
        process.join(timeout=3);assert process.exitcode==0
        assert yaml.safe_load(path.read_text())==latest
        expected=[initial]+([stale] if failed_file=='state_after_restart' else [])
        if newer=='aba':expected.append({'streams':{'synthetic':'intermediate'}})
        expected.append(latest)
        assert [json.loads(line) for line in receipts.read_text().splitlines()]==expected
    finally:
        if process.is_alive():process.terminate();process.join(timeout=1)
