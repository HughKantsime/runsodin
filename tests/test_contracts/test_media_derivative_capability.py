import os
import pytest
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_media_worker import file_facts
from core.media_derivative_capability import MediaDerivativeCapability,prepare,worker_command,validate,validate_envelope
from core.media_process_capability import identity

@pytest.fixture
def cap(tmp_path):
    os.chmod(tmp_path,0o700)
    source=os.open(tmp_path/'source.mp4',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
    os.write(source,b'fixture source bytes');os.close(source)
    source=os.open(tmp_path/'source.mp4',os.O_RDONLY)
    output=os.open(tmp_path/'output.mp4',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
    with acquire_media_guard(tmp_path,1) as guard:
        value=MediaDerivativeCapability.from_receipts(guard,source,output,file_facts(source),file_facts(output))
        yield value
    os.close(source);os.close(output)

def test_exact_fixed_typed_duplicate_transport(cap):
    replacements,copies,record=prepare(cap,worker_command())
    try:
        assert record['roles']==['guard','source','output']
        assert validate_envelope(record)==copies
        assert all(copy!=original for copy,original in zip(copies,cap.descriptors))
        assert len(replacements)==3
    finally:
        for fd in copies:os.close(fd)

@pytest.mark.parametrize('argv',[['ffmpeg','x'],['/bin/sh','x'],['python','worker','/raw/path']])
def test_no_arbitrary_command(cap,argv):
    with pytest.raises(ValueError,match='Fixed'):prepare(cap,argv)

def test_source_write_access_refused(tmp_path):
    fd=os.open(tmp_path/'source',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
    try:
        os.write(fd,b'x')
        with pytest.raises(ValueError,match='access'):validate(fd,'source')
    finally:os.close(fd)

def test_changed_output_cannot_arm(cap):
    os.write(cap.descriptors[2],b'x')
    with pytest.raises(ValueError):prepare(cap,worker_command())

def test_changed_source_identity_cannot_arm(cap):
    os.chmod(cap.descriptors[1],0o644)
    with pytest.raises(ValueError):prepare(cap,worker_command())

def test_foreign_roles_refused(cap):
    _,fds,record=prepare(cap,worker_command())
    try:
        record['roles']=['guard','namespace','output']
        with pytest.raises(ValueError):validate_envelope(record)
    finally:
        for fd in fds:os.close(fd)
