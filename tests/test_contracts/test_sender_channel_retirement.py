"""Group absence must not hide independently uncertain descriptor cleanup."""
import os,sys,time
import pytest
import core.sender_process_group as module
from tests.test_contracts.test_sender_process_group import cleanup_for_test


class CloseFault:
    def __init__(self,pipe,label,calls,fail):self.pipe=pipe;self.label=label;self.calls=calls;self.fail=fail
    def __getattr__(self,name):return getattr(self.pipe,name)
    def close(self):
        self.calls.append(self.label);self.pipe.close()
        if self.fail:raise OSError('synthetic private cleanup marker')


@pytest.mark.parametrize('faults',[{'stdin'},{'stdout'},{'stdin','stdout'}])
def test_group_absence_retained_but_channels_uncertain_and_all_closes_attempted(faults):
    group=module.OwnedSenderGroup([sys.executable,'-c','pass']);calls=[]
    try:
        assert group.wait_worker(time.monotonic()+2).worker_exited_normally
        for name in ('stdin','stdout'):
            setattr(group._process,name,CloseFault(getattr(group._process,name),name,calls,name in faults))
        receipt=group.retire()
        assert receipt.group_empty
        assert receipt.channels_closed is False
        assert calls==['stdin','stdout']
        assert 'private cleanup marker' not in repr(receipt)
        assert group.retire() is receipt and calls==['stdin','stdout']
    finally:group.retire();cleanup_for_test(group)


def test_normal_exit_group_and_control_channels_independently_confirmed():
    group=module.OwnedSenderGroup([sys.executable,'-c','pass'])
    try:
        assert group.wait_worker(time.monotonic()+2).worker_exited_normally
        receipt=group.retire()
        assert receipt.group_empty and receipt.channels_closed
    finally:group.retire();cleanup_for_test(group)


def test_pending_duplicate_close_uncertainty_survives_absent_group():
    group=module.OwnedSenderGroup([sys.executable,'-c','pass'])
    try:
        group._uncertain_channel_fds.append(999999) # Synthetic, never retried.
        receipt=group.retire()
        assert receipt.group_empty and not receipt.channels_closed
    finally:group.retire();cleanup_for_test(group)


def test_failed_startup_retires_after_private_duplicate_cleanup(monkeypatch):
    a,b=os.pipe();c,d=os.pipe();duplicates=[];closed=[];ordering=[];owned=None
    prepare=module._prepare_channels;close=module.os.close;retire=module.OwnedSenderGroup.retire
    def capture(*args):
        argv,fds=prepare(*args);duplicates.extend(fds);return argv,fds
    def close_fault(fd):
        close(fd)
        if fd in duplicates:
            closed.append(fd)
            if fd==duplicates[0]:raise OSError('synthetic private duplicate marker')
    def retire_record(group):ordering.append(set(duplicates)<=set(closed));return retire(group)
    monkeypatch.setattr(module,'_prepare_channels',capture)
    monkeypatch.setattr(module.os,'close',close_fault)
    monkeypatch.setattr(module.OwnedSenderGroup,'_read_ack',lambda *args:(_ for _ in ()).throw(RuntimeError('synthetic ready fault')))
    monkeypatch.setattr(module.OwnedSenderGroup,'retire',retire_record)
    try:
        with pytest.raises(module.OwnedGroupStartupError) as caught:
            module.OwnedSenderGroup([sys.executable,'-c','pass','{ODIN_INPUT_FD}','{ODIN_RESULT_FD}'],
                defer_start=True,channels={'input':a,'result':d})
        owned=caught.value.group
        assert ordering and all(ordering)
        assert caught.value.retirement.group_empty
        assert not caught.value.retirement.channels_closed
        assert set(duplicates)==set(closed)
    finally:
        monkeypatch.undo()
        if owned is not None:owned.retire();cleanup_for_test(owned)
        for fd in (a,b,c,d):os.close(fd)
