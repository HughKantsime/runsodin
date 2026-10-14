"""Owned synthetic POSIX workers only; no printer, credentials or external I/O."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from core.sender_process_group import OwnedSenderGroup


def ready(path):
    deadline=time.monotonic()+3
    while not path.exists():
        assert time.monotonic()<deadline,'Owned worker did not start'
        time.sleep(.01)


def worker(path,after=None):
    source="from pathlib import Path;import time;Path(%r).write_text('ready');time.sleep(1);"%str(path)
    source+=("Path(%r).write_text('command');"%str(after)) if after else ''
    source+='time.sleep(30)'
    return [sys.executable,'-c',source]


def cleanup_for_test(group):
    # Test-only teardown of the exact retained handle after deliberate refusal.
    # Production ownership is never exposed as an API or arbitrary PID kill.
    process=group._process
    if process.returncode is None:
        try:os.kill(-process.pid,signal.SIGKILL)
        except (ProcessLookupError,PermissionError):pass
    process.wait(timeout=3)


def test_owned_worker_retired_before_later_command_and_sibling_untouched(tmp_path):
    started=tmp_path/'ready';command=tmp_path/'command';sibling_ready=tmp_path/'sibling'
    group=OwnedSenderGroup(worker(started,command))
    sibling=subprocess.Popen(worker(sibling_ready),start_new_session=True)
    try:
        ready(started);ready(sibling_ready)
        receipt=group.retire()
        assert receipt.group_empty
        assert sibling.poll() is None
        time.sleep(1.1)
        assert not command.exists()
        assert group._process.returncode is not None
    finally:
        group.retire();cleanup_for_test(group);sibling.kill();sibling.wait(timeout=3)


def test_concurrent_retirement_signals_owned_group_only_once(tmp_path,monkeypatch):
    path=tmp_path/'ready';group=OwnedSenderGroup(worker(path));ready(path)
    real=os.killpg;calls=[]
    def tracked(pid,sig):
        if sig!=0:calls.append((pid,sig))
        return real(pid,sig)
    monkeypatch.setattr(os,'killpg',tracked)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:receipts=list(pool.map(lambda _:group.retire(),range(4)))
        assert all(r.group_empty for r in receipts)
        assert all(r is receipts[0] for r in receipts)
        assert calls==[(group._process.pid,signal.SIGKILL)]
    finally:cleanup_for_test(group)


def test_exited_worker_keeps_owned_anchor_alive(tmp_path):
    path=tmp_path/'exited'
    group=OwnedSenderGroup([sys.executable,'-c',"from pathlib import Path;Path(%r).write_text('done')"%str(path)])
    ready(path);time.sleep(.1)
    try:assert group.retire().group_empty
    finally:cleanup_for_test(group)


def test_external_reaper_violation_never_signals_reused_identity(tmp_path,monkeypatch):
    group=OwnedSenderGroup([sys.executable,'-c','pass'])
    group._process.kill();group._process.wait(timeout=3)
    calls=[];monkeypatch.setattr(os,'killpg',lambda *args:calls.append(args))
    receipt=group.retire()
    assert not receipt.group_empty and calls==[]
    assert group.retire() is receipt


def test_group_probe_uncertainty_is_not_positive_cleanup(tmp_path,monkeypatch):
    path=tmp_path/'ready';group=OwnedSenderGroup(worker(path),cleanup_seconds=.2);ready(path)
    real=os.killpg;signals=[]
    def uncertain(pid,sig):
        if sig==0:raise PermissionError('synthetic-private-marker')
        signals.append(sig);return real(pid,sig)
    monkeypatch.setattr(os,'killpg',uncertain)
    receipt=group.retire()
    assert not receipt.group_empty and 'synthetic-private-marker' not in repr(receipt)
    assert group.retire() is receipt and signals==[signal.SIGKILL]
    cleanup_for_test(group)


@pytest.mark.parametrize('budget',[True,0,-1,float('nan'),float('inf'),11])
def test_invalid_budget_refused_before_launch(monkeypatch,budget):
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('Invalid input launched worker'))
    with pytest.raises(ValueError):OwnedSenderGroup(['synthetic'],cleanup_seconds=budget)


def test_custom_sigchld_refuses_launch(monkeypatch):
    monkeypatch.setattr(signal,'getsignal',lambda *args:signal.SIG_IGN)
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('Automatic reaping launched worker'))
    with pytest.raises(RuntimeError):OwnedSenderGroup(['synthetic'])

@pytest.mark.parametrize('leader_exits',[False,True])
def test_owned_group_descendant_cannot_issue_later_command(tmp_path,leader_exits):
    path=tmp_path/'descendant-ready';command=tmp_path/'late-command'
    child="from pathlib import Path;import time;time.sleep(1);Path(%r).write_text('command');time.sleep(30)"%str(command)
    source="import subprocess,sys,time,os;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c',%r]);Path(%r).write_text(str(p.pid));"%(child,str(path))
    source+='os._exit(0)' if leader_exits else 'time.sleep(30)'
    group=OwnedSenderGroup([sys.executable,'-c',source],cleanup_seconds=3)
    try:
        ready(path)
        if leader_exits:time.sleep(.1)
        assert group.retire().group_empty
        time.sleep(1.1)
        assert not command.exists()
    finally:
        group.retire();cleanup_for_test(group)


def test_signal_failure_remains_uncertain_and_worker_is_not_falsely_retired(tmp_path,monkeypatch):
    path=tmp_path/'ready';group=OwnedSenderGroup(worker(path));ready(path)
    real=os.killpg
    def denied(pid,sig):
        if sig!=0:raise PermissionError('synthetic-private-marker')
        return real(pid,sig)
    monkeypatch.setattr(os,'killpg',denied)
    try:
        receipt=group.retire()
        assert not receipt.group_empty
        assert group._process.returncode is None
        assert 'synthetic-private-marker' not in repr(receipt)
    finally:cleanup_for_test(group)


def test_unexpected_anchor_exit_is_not_positive_quiescence():
    group=OwnedSenderGroup([sys.executable,'-c','pass'])
    time.sleep(.15)
    group._process.kill()
    time.sleep(.1)
    try:
        assert not group.retire().group_empty
    finally:cleanup_for_test(group)
