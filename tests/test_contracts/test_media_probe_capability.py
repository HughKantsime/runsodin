import os
import pytest
from tests.test_contracts.test_media_derivative_capability import cap
from core.media_probe_capability import MediaProbeCapability,prepare,worker_command,validate_envelope
from core.media_process_dispatch import prepare as dispatch

def test_probe_is_exact_readonly_two_roles(cap):
    probe=MediaProbeCapability.from_receipts(cap.guard,cap.descriptors[1],dict(cap.facts[1]))
    _,fds,record=dispatch(probe,worker_command())
    try:
        assert record['roles']==['guard','source']
        assert validate_envelope(record)==fds
        assert len(fds)==2
    finally:
        for fd in fds:os.close(fd)

def test_probe_cannot_receive_output_or_extra_role(cap):
    with pytest.raises(ValueError):MediaProbeCapability.from_receipts(cap.guard,cap.descriptors[2],dict(cap.facts[2]))
    probe=MediaProbeCapability.from_receipts(cap.guard,cap.descriptors[1],dict(cap.facts[1]))
    _,fds,record=prepare(probe,worker_command())
    try:
        record['roles'].append('output')
        with pytest.raises(ValueError):validate_envelope(record)
    finally:
        for fd in fds:os.close(fd)

def test_probe_cannot_run_raw_command(cap):
    probe=MediaProbeCapability.from_receipts(cap.guard,cap.descriptors[1],dict(cap.facts[1]))
    with pytest.raises(ValueError):prepare(probe,['/usr/bin/ffprobe','/tmp/raw'])
