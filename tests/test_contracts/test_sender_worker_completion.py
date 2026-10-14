"""Exact owned worker exit, separate from result EOF and group retirement."""
import os
import signal
import sys
import time
import pytest
from core.sender_process_group import OwnedSenderGroup
from tests.test_contracts.test_sender_process_group import cleanup_for_test


def group(source,**kwargs):return OwnedSenderGroup([sys.executable,'-c',source],**kwargs)


@pytest.mark.parametrize('source,normal',[
    ('pass',True),('import sys;sys.exit(2)',False),
    ('import os,signal;os.kill(os.getpid(),signal.SIGTERM)',False)])
def test_actual_exact_worker_exit(source,normal):
    owned=group(source,defer_start=True)
    try:
        assert owned.arm().worker_started
        receipt=owned.wait_worker(time.monotonic()+2)
        assert receipt.worker_exited_normally is normal
        assert owned.wait_worker(time.monotonic()+2) is receipt
        assert owned.retire().group_empty
        assert owned.wait_worker(time.monotonic()+2) is receipt
    finally:owned.retire();cleanup_for_test(owned)


def test_before_arm_wait_never_allows_future_worker(tmp_path):
    marker=tmp_path/'command'
    owned=group('from pathlib import Path;Path(%r).write_text("command")'%str(marker),defer_start=True)
    try:
        assert not owned.wait_worker(time.monotonic()+1).worker_exited_normally
        assert not owned.arm().command_may_have_started
        assert not marker.exists()
        assert owned.retire().group_empty
    finally:owned.retire();cleanup_for_test(owned)


def test_retired_group_cannot_claim_worker_completed():
    owned=group('pass',defer_start=True)
    try:
        assert owned.retire().group_empty
        assert not owned.wait_worker(time.monotonic()+1).worker_exited_normally
    finally:cleanup_for_test(owned)


def test_hung_and_later_success_cannot_upgrade_failed_receipt():
    owned=group('import time;time.sleep(.3)',defer_start=True)
    try:
        assert owned.arm().worker_started
        receipt=owned.wait_worker(time.monotonic()+.02)
        assert not receipt.worker_exited_normally
        time.sleep(.4)
        assert owned.wait_worker(time.monotonic()+1) is receipt
        assert owned.retire().group_empty
    finally:owned.retire();cleanup_for_test(owned)


def test_private_result_eof_before_nonzero_exit_is_not_normal_completion():
    r,w=os.pipe();rr,rw=os.pipe();owned=None
    source='import os,sys,time;os.write(int(sys.argv[2]),b"reply");os.close(int(sys.argv[2]));time.sleep(.1);sys.exit(2)'
    try:
        owned=OwnedSenderGroup([sys.executable,'-c',source,'{ODIN_INPUT_FD}','{ODIN_RESULT_FD}'],
            defer_start=True,channels={'input':r,'result':rw})
        os.close(r);r=None;os.close(rw);rw=None
        assert owned.arm().worker_started
        assert os.read(rr,10)==b'reply'
        assert os.read(rr,1)==b''
        assert not owned.wait_worker(time.monotonic()+2).worker_exited_normally
        assert owned.retire().group_empty
    finally:
        if owned is not None:owned.retire();cleanup_for_test(owned)
        for fd in (r,w,rr,rw):
            if fd is not None:os.close(fd)


@pytest.mark.parametrize('deadline',[True,0,float('nan'),float('inf')])
def test_invalid_completion_deadline_no_wait_permission(deadline):
    owned=group('pass',defer_start=True)
    try:
        assert owned.arm().worker_started
        assert not owned.wait_worker(deadline).worker_exited_normally
    finally:owned.retire();cleanup_for_test(owned)


def test_pending_private_descriptor_uncertainty_not_normal_proof():
    owned=group('pass',defer_start=True)
    try:
        assert owned.arm().worker_started
        owned._uncertain_channel_fds.append(999999) # Synthetic fault, never closed/retried.
        assert not owned.wait_worker(time.monotonic()+2).worker_exited_normally
    finally:owned.retire();cleanup_for_test(owned)


def test_completion_rejected_if_deadline_passes_at_ack_acceptance(monkeypatch):
    import core.sender_process_group as module
    owned=group('pass',defer_start=True)
    try:
        assert owned.arm().worker_started
        now=time.monotonic();deadline=now+1
        def ack(_):monkeypatch.setattr(module.time,'monotonic',lambda:deadline);return b'EXIT_OK\n'
        monkeypatch.setattr(owned,'_read_ack',ack)
        assert not owned.wait_worker(deadline).worker_exited_normally
    finally:
        monkeypatch.undo();owned.retire();cleanup_for_test(owned)


def test_unsolicited_bad_completion_ack_is_refused(monkeypatch):
    owned=group('pass',defer_start=True)
    try:
        assert owned.arm().worker_started
        monkeypatch.setattr(owned,'_read_ack',lambda _:b'EXIT_OK\nEXIT_OK\n')
        assert not owned.wait_worker(time.monotonic()+1).worker_exited_normally
    finally:owned.retire();cleanup_for_test(owned)


def test_fragmented_wait_control_is_accumulated(monkeypatch):
    owned=group('pass',defer_start=True)
    try:
        assert owned.arm().worker_started
        def fragmented(message,deadline,**kwargs):
            assert message==b'WAIT\n'
            os.write(owned._process.stdin.fileno(),b'WA');time.sleep(.08)
            os.write(owned._process.stdin.fileno(),b'IT\n')
        monkeypatch.setattr(owned,'_write_message',fragmented)
        assert owned.wait_worker(time.monotonic()+2).worker_exited_normally
    finally:owned.retire();cleanup_for_test(owned)


def test_real_extra_wait_bytes_do_not_confirm_exit(monkeypatch):
    owned=group('pass',defer_start=True)
    try:
        assert owned.arm().worker_started
        monkeypatch.setattr(owned,'_write_message',lambda *args,**kwargs:os.write(owned._process.stdin.fileno(),b'WAIT\nX'))
        assert not owned.wait_worker(time.monotonic()+.15).worker_exited_normally
        assert owned.retire().group_empty
    finally:owned.retire();cleanup_for_test(owned)


def test_expiry_after_actual_completion_read_is_refused(monkeypatch):
    import core.sender_process_group as module
    owned=group('pass',defer_start=True)
    try:
        assert owned.arm().worker_started
        deadline=time.monotonic()+1
        original=module.os.read
        def expired_read(fd,size):
            value=original(fd,size)
            if fd==owned._process.stdout.fileno() and value:
                monkeypatch.setattr(module.time,'monotonic',lambda:deadline)
            return value
        monkeypatch.setattr(module.os,'read',expired_read)
        assert not owned.wait_worker(deadline).worker_exited_normally
    finally:monkeypatch.undo();owned.retire();cleanup_for_test(owned)


def test_retirement_waits_only_for_completion_budget(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    owned=group('import time;time.sleep(30)',defer_start=True)
    try:
        assert owned.arm().worker_started
        started=threading.Event();original=owned._write_message
        def track(*args,**kwargs):original(*args,**kwargs);started.set()
        monkeypatch.setattr(owned,'_write_message',track)
        now=time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            completion=pool.submit(owned.wait_worker,now+.15)
            assert started.wait(1)
            retirement=pool.submit(owned.retire)
            assert not completion.result(timeout=1).worker_exited_normally
            assert retirement.result(timeout=1).group_empty
        assert time.monotonic()-now<1
    finally:owned.retire();cleanup_for_test(owned)


def test_normal_worker_exit_does_not_imply_descendant_absence(tmp_path):
    from tests.test_contracts.test_sender_process_group import ready
    started=tmp_path/'child';later=tmp_path/'command'
    child='from pathlib import Path;import time;Path(%r).write_text("ready");time.sleep(1);Path(%r).write_text("command");time.sleep(30)'%(str(started),str(later))
    source='import subprocess,sys;subprocess.Popen([sys.executable,"-c",%r])'%child
    owned=group(source,defer_start=True)
    try:
        assert owned.arm().worker_started
        ready(started)
        assert owned.wait_worker(time.monotonic()+2).worker_exited_normally
        assert not later.exists()
        assert owned.retire().group_empty
        time.sleep(1.1)
        assert not later.exists()
    finally:owned.retire();cleanup_for_test(owned)
