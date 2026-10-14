"""Owned anonymous pipes only, generated private data never logged."""
import hashlib,os,select,sys
from core.sender_process_group import OwnedSenderGroup
from tests.test_contracts.test_sender_process_group import cleanup_for_test


def test_owned_worker_private_pipe_transfer_not_argv():
    reader,writer=os.pipe();result_reader,result_writer=os.pipe();group=None
    source="import os,sys,hashlib;data=os.read(int(sys.argv[1]),1024);os.write(int(sys.argv[2]),hashlib.sha256(data).digest())"
    try:
        group=OwnedSenderGroup([sys.executable,'-c',source,'{ODIN_INPUT_FD}','{ODIN_RESULT_FD}'],
            defer_start=True,channels={'input':reader,'result':result_writer})
        os.close(reader);reader=None;os.close(result_writer);result_writer=None
        assert select.select([result_reader],[],[],.1)[0]==[]
        assert group.arm().worker_started
        data=os.urandom(64);os.write(writer,data);os.close(writer);writer=None
        assert select.select([result_reader],[],[],2)[0]
        result=os.read(result_reader,64)
        assert result==hashlib.sha256(data).digest()
        assert select.select([result_reader],[],[],2)[0]
        assert os.read(result_reader,1)==b''
        assert group.retire().group_empty
    finally:
        if group is not None:group.retire();cleanup_for_test(group)
        for fd in (reader,writer,result_reader,result_writer):
            if fd is not None:os.close(fd)


import pytest
import core.sender_process_group as module


def pipes():
    a,b=os.pipe();c,d=os.pipe()
    return a,b,c,d


def test_failed_exec_closes_anchor_channels_so_result_has_eof(tmp_path):
    a,b,c,d=pipes();group=None
    try:
        group=OwnedSenderGroup([str(tmp_path/'missing'),' {unused}', '{ODIN_INPUT_FD}','{ODIN_RESULT_FD}'],
            defer_start=True,channels={'input':a,'result':d})
        os.close(a);a=None;os.close(d);d=None
        receipt=group.arm();assert not receipt.worker_started and not receipt.command_may_have_started
        assert select.select([c],[],[],2)[0] and os.read(c,1)==b''
        assert group.retire().group_empty
    finally:
        if group is not None:group.retire();cleanup_for_test(group)
        for fd in (a,b,c,d):
            if fd is not None:os.close(fd)


def test_cancel_deferred_group_closes_private_channels_without_worker():
    a,b,c,d=pipes();group=None
    try:
        group=OwnedSenderGroup([sys.executable,'-c','pass','{ODIN_INPUT_FD}','{ODIN_RESULT_FD}'],
            defer_start=True,channels={'input':a,'result':d})
        os.close(a);a=None;os.close(d);d=None
        assert group.retire().group_empty
        assert not group.arm().command_may_have_started
        assert select.select([c],[],[],2)[0] and os.read(c,1)==b''
    finally:
        if group is not None:group.retire();cleanup_for_test(group)
        for fd in (a,b,c,d):
            if fd is not None:os.close(fd)


@pytest.mark.parametrize('case',['same_pipe','wrong_direction','missing','shared_fd','partial_marker','extra_marker'])
def test_bad_channels_refuse_before_launch(monkeypatch,case):
    a,b,c,d=pipes()
    argv=[sys.executable,'-c','pass','{ODIN_INPUT_FD}','{ODIN_RESULT_FD}']
    channels={'input':a,'result':d}
    if case=='same_pipe':channels['result']=b
    if case=='wrong_direction':channels={'input':b,'result':c}
    if case=='missing':channels={'input':a}
    if case=='shared_fd':channels['result']=a
    if case=='partial_marker':argv[-1]='prefix{ODIN_RESULT_FD}'
    if case=='extra_marker':argv.append('{ODIN_EXTRA_FD}')
    monkeypatch.setattr(module.subprocess,'Popen',lambda *args,**kwargs:pytest.fail('Invalid channels launched'))
    try:
        with pytest.raises(ValueError):OwnedSenderGroup(argv,channels=channels)
        assert os.fstat(a) and os.fstat(d)
    finally:
        for fd in (a,b,c,d):os.close(fd)


def test_duplicate_failure_closes_copies_preserving_caller(monkeypatch):
    a,b,c,d=pipes();real=module.os.dup;copies=[]
    def duplicate(fd):
        if copies:raise OSError('private-marker')
        copy=real(fd);copies.append(copy);return copy
    monkeypatch.setattr(module.os,'dup',duplicate)
    monkeypatch.setattr(module.subprocess,'Popen',lambda *args,**kwargs:pytest.fail('Failed duplication launched'))
    try:
        with pytest.raises(ValueError) as caught:
            OwnedSenderGroup([sys.executable,'{ODIN_INPUT_FD}','{ODIN_RESULT_FD}'],channels={'input':a,'result':d})
        assert 'private-marker' not in str(caught.value)
        with pytest.raises(OSError):os.fstat(copies[0])
        assert os.fstat(a) and os.fstat(d)
    finally:
        for fd in (a,b,c,d):os.close(fd)


def test_named_fifo_is_not_accepted_as_anonymous_pipe(tmp_path,monkeypatch):
    path=tmp_path/'named';os.mkfifo(path);named=os.open(path,os.O_RDONLY|os.O_NONBLOCK);a,b,c,d=pipes()
    monkeypatch.setattr(module.subprocess,'Popen',lambda *args,**kwargs:pytest.fail('Named channel launched'))
    try:
        with pytest.raises(ValueError):OwnedSenderGroup([sys.executable,'{ODIN_INPUT_FD}','{ODIN_RESULT_FD}'],channels={'input':named,'result':d})
    finally:
        for fd in (named,a,b,c,d):os.close(fd)


@pytest.mark.parametrize('ready_failure',[False,True])
@pytest.mark.parametrize('deny_signal',[False,True])
def test_duplicate_close_failure_retains_group_and_attempts_every_close(monkeypatch,ready_failure,deny_signal):
    a,b,c,d=pipes();real_duplicate=module.os.dup;real_close=module.os.close;real_signal=module.os.killpg
    copies=[];attempts=[];group=None
    def duplicate(fd):
        value=real_duplicate(fd);copies.append(value);return value
    def close(fd):
        if fd in copies:
            attempts.append(fd)
            if fd==copies[0]:raise OSError('private-marker')
        return real_close(fd)
    def denied(pid,sig):
        if sig:raise PermissionError('private-marker')
        return real_signal(pid,sig)
    monkeypatch.setattr(module.os,'dup',duplicate)
    monkeypatch.setattr(module.os,'close',close)
    if deny_signal:monkeypatch.setattr(module.os,'killpg',denied)
    if ready_failure:
        monkeypatch.setattr(OwnedSenderGroup,'_read_ack',lambda *args:(_ for _ in ()).throw(TimeoutError('private-marker')))
    try:
        with pytest.raises(module.OwnedGroupStartupError) as caught:
            OwnedSenderGroup([sys.executable,'-c','pass','{ODIN_INPUT_FD}','{ODIN_RESULT_FD}'],
                channels={'input':a,'result':d},defer_start=True)
        group=caught.value.group
        assert caught.value.retirement.group_empty is (not deny_signal)
        assert attempts==copies and len(copies)==2
        assert 'private-marker' not in repr(caught.value)
        assert (group._process.returncode is None) is deny_signal
        assert group._uncertain_channel_fds==copies[:1]
        assert os.fstat(a) and os.fstat(d)
    finally:
        if group is not None:cleanup_for_test(group)
        for fd in copies:
            try:real_close(fd)
            except OSError:pass
        for fd in (a,b,c,d):real_close(fd)


def test_validation_close_failure_retains_unconfirmed_descriptor_without_launch(monkeypatch):
    a,b,c,d=pipes();real_duplicate=module.os.dup;real_close=module.os.close;copies=[];group=None
    def duplicate(fd):
        if copies:raise OSError('private-marker')
        value=real_duplicate(fd);copies.append(value);return value
    def close(fd):
        if fd in copies:raise OSError('private-marker')
        return real_close(fd)
    monkeypatch.setattr(module.os,'dup',duplicate)
    monkeypatch.setattr(module.os,'close',close)
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k:pytest.fail('Failed channels launched'))
    try:
        with pytest.raises(module.OwnedGroupStartupError) as caught:
            OwnedSenderGroup([sys.executable,'{ODIN_INPUT_FD}','{ODIN_RESULT_FD}'],channels={'input':a,'result':d})
        group=caught.value.group
        assert group._process is None and not caught.value.retirement.group_empty
        assert group._uncertain_channel_fds==copies
        assert 'private-marker' not in repr(caught.value)
    finally:
        for fd in copies+[a,b,c,d]:real_close(fd)
