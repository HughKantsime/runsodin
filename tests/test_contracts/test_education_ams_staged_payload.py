"""Real bounded staged archive verifies bytes before approval, no print fixture."""
import hashlib
from pathlib import Path
import pytest
from modules.printers.services import inspect_ams_payload


def test_staged_genuine_source_fixture_retains_exact_hash_and_source(tmp_path):
    payload=(Path(__file__).resolve().parents[2]/'tests/fixtures/telemetry/a1-petg-source-sanitized.3mf').read_bytes()
    path=tmp_path/'owned.3mf';path.write_bytes(payload)
    digest=hashlib.sha256(payload).hexdigest()
    result=inspect_ams_payload(str(path),expected_sha256=digest,expected_bytes=len(payload),plate_index=0)
    assert result=={'payload_sha256':digest,'storage_bytes':len(payload),'plate_index':0,'source_index':0,'material':'PETG'}
    assert not {'path','bytes','gcode','stored_path'} & result.keys()


@pytest.mark.parametrize('change',['bytes','size','plate'])
def test_changed_staged_bytes_or_size_or_plate_refuse(tmp_path,change):
    payload=(Path(__file__).resolve().parents[2]/'tests/fixtures/telemetry/a1-petg-source-sanitized.3mf').read_bytes()
    path=tmp_path/'owned.3mf';path.write_bytes(payload)
    digest=hashlib.sha256(payload).hexdigest()
    if change=='bytes':path.write_bytes(payload+b'changed')
    with pytest.raises((ValueError,RuntimeError)):
        inspect_ams_payload(str(path),expected_sha256=digest,expected_bytes=len(payload)+(1 if change=='size' else 0),plate_index=1 if change=='plate' else 0)
