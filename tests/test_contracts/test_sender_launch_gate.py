"""Actual owned synthetic launch gating, no device or external service calls."""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
import core.sender_process_group as supervisor
from tests.test_contracts.test_sender_process_group import cleanup_for_test, ready


def command(path):
    return [sys.executable,'-c',"from pathlib import Path;import time;Path(%r).write_text('ran');time.sleep(30)"%str(path)]


def deferred_group(argv,*,arm_seconds):
    # Python anchor startup needs the default budget even when arm must time out
    # quickly. Narrow the shared budget only after READY confirms construction.
    group=supervisor.OwnedSenderGroup(argv,defer_start=True)
    group._startup_seconds=arm_seconds
    return group


def test_deferred_start_has_no_worker_until_explicit_arm(tmp_path):
    path=tmp_path/'command'
    group=supervisor.OwnedSenderGroup(command(path),defer_start=True)
    try:
        time.sleep(.15)
        assert not path.exists()
        receipt=group.arm()
        assert receipt.worker_started and receipt.command_may_have_started
        ready(path)
        assert group.arm() is receipt
    finally:group.retire();cleanup_for_test(group)


def test_cancel_before_arm_never_starts_worker(tmp_path):
    path=tmp_path/'command'
    group=supervisor.OwnedSenderGroup(command(path),defer_start=True)
    try:
        assert group.retire().group_empty
        receipt=group.arm()
        assert not receipt.worker_started and not receipt.command_may_have_started
        time.sleep(.1)
        assert not path.exists()
    finally:cleanup_for_test(group)


def test_concurrent_arm_starts_only_one_command(tmp_path):
    path=tmp_path/'commands'
    argv=[sys.executable,'-c',"from pathlib import Path;import time;p=Path(%r);p.open('a').write('ran\\n');time.sleep(30)"%str(path)]
    group=supervisor.OwnedSenderGroup(argv,defer_start=True)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts=list(pool.map(lambda _:group.arm(),range(4)))
        assert all(r is receipts[0] for r in receipts)
        assert receipts[0].worker_started
        ready(path)
        assert path.read_text()=='ran\n'
    finally:group.retire();cleanup_for_test(group)


def test_failed_exec_is_fixed_ack_not_success(tmp_path):
    group=supervisor.OwnedSenderGroup([str(tmp_path/'absent-private-marker')],defer_start=True)
    try:
        receipt=group.arm()
        assert not receipt.worker_started
        assert not receipt.command_may_have_started
        assert 'private-marker' not in repr(receipt)
        assert group.retire().group_empty
    finally:cleanup_for_test(group)


@pytest.mark.parametrize('budget',[True,0,-1,float('nan'),float('inf'),11])
def test_invalid_ipc_budget_refuses_before_launch(monkeypatch,budget):
    monkeypatch.setattr(supervisor.subprocess,'Popen',lambda *a,**k:pytest.fail('Launched'))
    with pytest.raises(ValueError):supervisor.OwnedSenderGroup(['synthetic'],startup_seconds=budget)


def test_lost_start_ack_retains_uncertain_receipt(tmp_path,monkeypatch):
    path=tmp_path/'command'
    group=deferred_group(command(path),arm_seconds=.15)
    original=group._read_ack
    monkeypatch.setattr(group,'_read_ack',lambda deadline: (_ for _ in ()).throw(TimeoutError('private-marker')))
    try:
        receipt=group.arm()
        assert not receipt.worker_started and receipt.command_may_have_started
        assert 'private-marker' not in repr(receipt)
        ready(path)
        assert group.arm() is receipt
    finally:
        monkeypatch.setattr(group,'_read_ack',original)
        group.retire();cleanup_for_test(group)


def test_retirement_serializes_with_arm(tmp_path):
    path=tmp_path/'command'
    group=supervisor.OwnedSenderGroup(command(path),defer_start=True)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            arm=pool.submit(group.arm)
            retired=pool.submit(group.retire)
            result=arm.result();receipt=retired.result()
        assert receipt.group_empty
        assert group.arm() is result
        before=path.exists()
        time.sleep(.1)
        assert path.exists()==before
    finally:cleanup_for_test(group)


def synthetic_anchor(monkeypatch,source):
    real=supervisor.subprocess.Popen
    def launch(argv,**kwargs):
        return real([sys.executable,'-c',source],**kwargs)
    monkeypatch.setattr(supervisor.subprocess,'Popen',launch)


def test_ready_timeout_retains_exact_owned_handle_and_sanitizes(tmp_path,monkeypatch):
    synthetic_anchor(monkeypatch,'import time;time.sleep(30)')
    started=time.monotonic()
    with pytest.raises(supervisor.OwnedGroupStartupError) as caught:
        supervisor.OwnedSenderGroup(command(tmp_path/'private-marker'),startup_seconds=.1)
    failure=caught.value
    try:
        assert time.monotonic()-started<1.5
        assert failure.retirement.group_empty
        assert failure.group._process.returncode is not None
        assert 'private-marker' not in repr(failure)
    finally:cleanup_for_test(failure.group)


def test_large_config_write_timeout_is_bounded(monkeypatch):
    synthetic_anchor(monkeypatch,'import time;time.sleep(30)')
    started=time.monotonic()
    with pytest.raises(supervisor.OwnedGroupStartupError) as caught:
        supervisor.OwnedSenderGroup(['synthetic','x'*60000],startup_seconds=.1)
    try:
        assert time.monotonic()-started<1.5
        assert caught.value.retirement.group_empty
    finally:cleanup_for_test(caught.value.group)


@pytest.mark.parametrize('ack',["b'WRONG\\n'","b'READY\\nEXTRA'","b'R'*40","b'REA'"])
def test_malformed_or_partial_ready_never_arms(monkeypatch,tmp_path,ack):
    source="import sys,time;sys.stdin.buffer.readline();sys.stdout.buffer.write(%s);sys.stdout.buffer.flush();time.sleep(30)"%ack
    synthetic_anchor(monkeypatch,source)
    with pytest.raises(supervisor.OwnedGroupStartupError) as caught:
        supervisor.OwnedSenderGroup(command(tmp_path/'command'))
    try:
        assert not caught.value.group._arm_delivered
        assert caught.value.retirement.group_empty
        assert not (tmp_path/'command').exists()
    finally:cleanup_for_test(caught.value.group)


def test_anchor_dies_after_ready_before_arm(tmp_path):
    group=supervisor.OwnedSenderGroup(command(tmp_path/'command'),defer_start=True)
    try:
        group._process.kill()
        deadline=time.monotonic()+2
        # Observe exit without reaping: retirement must retain the owned PID.
        while not supervisor._check_unreaped_child(group._process.pid):
            if time.monotonic()>=deadline:
                raise TimeoutError('Killed anchor did not exit')
            time.sleep(.01)
        receipt=group.arm()
        assert not receipt.worker_started and not receipt.command_may_have_started
        assert not group.retire().group_empty
        assert not (tmp_path/'command').exists()
    finally:cleanup_for_test(group)


def test_actual_arm_ack_timeout_is_bounded_and_uncertain(monkeypatch,tmp_path):
    source="import sys,time;sys.stdin.buffer.readline();sys.stdout.buffer.write(b'READY\\n');sys.stdout.buffer.flush();sys.stdin.buffer.readline();time.sleep(30)"
    synthetic_anchor(monkeypatch,source)
    group=deferred_group(command(tmp_path/'command'),arm_seconds=.15)
    try:
        started=time.monotonic();receipt=group.arm()
        assert time.monotonic()-started<1
        assert not receipt.worker_started and receipt.command_may_have_started
        assert group.retire().group_empty
        assert group.arm() is receipt
    finally:cleanup_for_test(group)


def test_anchor_dies_after_arm_keeps_command_uncertain(monkeypatch,tmp_path):
    source="import sys,os;sys.stdin.buffer.readline();sys.stdout.buffer.write(b'READY\\n');sys.stdout.buffer.flush();sys.stdin.buffer.readline();os._exit(0)"
    synthetic_anchor(monkeypatch,source)
    group=deferred_group(command(tmp_path/'command'),arm_seconds=.2)
    try:
        receipt=group.arm()
        assert not receipt.worker_started and receipt.command_may_have_started
        # The closed pipe can precede the exited-child state: observe the exit without
        # reaping, because this test covers a known unexpected exit.
        deadline=time.monotonic()+2
        while not supervisor._check_unreaped_child(group._process.pid):
            if time.monotonic()>=deadline:
                raise TimeoutError('Anchor did not exit')
            time.sleep(.01)
        assert not group.retire().group_empty
        assert group.arm() is receipt
    finally:cleanup_for_test(group)


def test_startup_failure_keeps_handle_when_retirement_unconfirmed(monkeypatch,tmp_path):
    synthetic_anchor(monkeypatch,'import time;time.sleep(30)')
    real=supervisor.os.killpg
    def denied(pid,sig):
        if sig:raise PermissionError('private-marker')
        return real(pid,sig)
    monkeypatch.setattr(supervisor.os,'killpg',denied)
    with pytest.raises(supervisor.OwnedGroupStartupError) as caught:
        supervisor.OwnedSenderGroup(command(tmp_path/'private-marker'),startup_seconds=.1)
    failure=caught.value
    try:
        assert not failure.retirement.group_empty
        assert failure.group._process.returncode is None
        assert not failure.group.arm().command_may_have_started
        assert 'private-marker' not in repr(failure)
    finally:cleanup_for_test(failure.group)


def test_delayed_writable_select_does_not_arm_after_deadline(tmp_path,monkeypatch):
    group=deferred_group(command(tmp_path/'command'),arm_seconds=.1)
    real=supervisor.select.select
    def delayed(readers,writers,errors,timeout):
        result=real(readers,writers,errors,timeout)
        if writers:time.sleep(.15)
        return result
    monkeypatch.setattr(supervisor.select,'select',delayed)
    try:
        receipt=group.arm()
        assert not receipt.worker_started and not receipt.command_may_have_started
        assert not (tmp_path/'command').exists()
    finally:group.retire();cleanup_for_test(group)


def test_delayed_ack_read_does_not_accept_late_started(tmp_path,monkeypatch):
    group=deferred_group(command(tmp_path/'command'),arm_seconds=.1)
    real=supervisor.os.read
    def delayed(fd,size):
        value=real(fd,size)
        time.sleep(.15)
        return value
    monkeypatch.setattr(supervisor.os,'read',delayed)
    try:
        receipt=group.arm()
        assert not receipt.worker_started and receipt.command_may_have_started
        ready(tmp_path/'command')
    finally:group.retire();cleanup_for_test(group)
