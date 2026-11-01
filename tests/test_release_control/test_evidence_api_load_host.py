"""The uploaded manifest keeps the host class and limits api_load used. The native result is not
uploaded, and the next checkout wipes it. Separate from the exact 34-case practical evidence file."""
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from ops.release_control import practical_evidence as evidence
from ops.release_control.practical_evidence import EvidenceError, build_bundle, verify_bundle
from tests.test_release_control.test_practical_evidence import SHA, _run, _write

ROOT = Path(__file__).parents[2]
LIMITS = {'read_p95_ms': 1450, 'write_p95_ms': 2550, 'global_p99_ms': 3850}
IDENTITY = {'system': 'Linux', 'machine': 'x86_64', 'hostname': 'odin-ci',
            'cpu': '11th Gen Intel(R) Core(TM) i7-11700K @ 3.60GHz'}


def _api_load(source, **changes):
    result = {'gate_id': 'api_load', 'status': 'pass',
              'metrics': {'host_class': 'odin-ci', 'latency_limits': LIMITS, 'host_identity': IDENTITY}}
    result.update(changes)
    _write(source / evidence.API_LOAD_RESULT, result)
    return source / evidence.API_LOAD_RESULT


def _rewrite(evidence_dir, manifest):
    data = evidence.canonical_json(manifest)
    (evidence_dir / 'manifest.json').write_bytes(data)
    (evidence_dir / 'manifest.sha256').write_text(f'{hashlib.sha256(data).hexdigest()}  manifest.json\n')


def test_manifest_keeps_the_class_and_limits_and_binds_them_to_the_native_result(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch)
    native = _api_load(source)
    output = build_bundle(source, SHA, record_api_load_host=True)
    manifest = verify_bundle(output)['manifest']
    assert manifest['api_load_host'] == {
        'host_class': 'odin-ci', 'latency_limits': LIMITS, 'host_identity': IDENTITY,
        'result_sha256': hashlib.sha256(native.read_bytes()).hexdigest(),
    }
    page = (output / 'index.html').read_text()
    assert 'odin-ci' in page and '1450 ms' in page and '3850 ms' in page


def test_nothing_is_recorded_unless_the_workflow_asks(tmp_path, monkeypatch):
    """A verifier older than this key rejects it, so the first candidate carrying this code must not send it."""
    source = _run(tmp_path, monkeypatch)
    _api_load(source)
    manifest = verify_bundle(build_bundle(source, SHA))['manifest']
    assert 'api_load_host' not in manifest


def test_the_workflow_asks_and_make_passes_it_on():
    workflow = yaml.safe_load((ROOT / '.github/workflows/trusted-validation.yml').read_text())
    step = next(step for step in workflow['jobs']['validate']['steps'] if step.get('name') == 'Build practical release evidence')
    assert 'RECORD_API_LOAD_HOST=1' in step['run']
    assert '$(if $(RECORD_API_LOAD_HOST),--record-api-load-host,)' in (ROOT / 'Makefile').read_text()


@pytest.mark.parametrize('prepare', ['missing', 'failed', 'no-class', 'no-limits', 'partial-limits', 'no-identity'])
def test_a_requested_record_cant_be_built_without_real_values(tmp_path, monkeypatch, prepare):
    source = _run(tmp_path, monkeypatch)
    if prepare == 'failed':
        _api_load(source, status='fail')
    elif prepare == 'no-class':
        _api_load(source, metrics={'host_class': None, 'latency_limits': None})
    elif prepare == 'no-limits':
        _api_load(source, metrics={'host_class': 'odin-ci', 'host_identity': IDENTITY})
    elif prepare == 'partial-limits':
        _api_load(source, metrics={'host_class': 'odin-ci', 'latency_limits': {'read_p95_ms': 1450}, 'host_identity': IDENTITY})
    elif prepare == 'no-identity':
        _api_load(source, metrics={'host_class': 'odin-ci', 'latency_limits': LIMITS})
    with pytest.raises(EvidenceError):
        build_bundle(source, SHA, record_api_load_host=True)


@pytest.mark.parametrize('tamper', ['digest', 'extra', 'limit-type', 'class-shape', 'dropped-limit'])
def test_verifier_rejects_a_record_that_was_altered(tmp_path, monkeypatch, tamper):
    source = _run(tmp_path, monkeypatch)
    _api_load(source)
    output = build_bundle(source, SHA, record_api_load_host=True)
    manifest = json.loads((output / 'manifest.json').read_text())
    host = manifest['api_load_host']
    if tamper == 'digest':
        host['result_sha256'] = '0' * 64
    elif tamper == 'extra':
        host['note'] = 'x'
    elif tamper == 'limit-type':
        host['latency_limits']['read_p95_ms'] = 'any'
    elif tamper == 'class-shape':
        host['host_class'] = 'Not A Class'
    elif tamper == 'dropped-limit':
        del host['latency_limits']['global_p99_ms']
    _rewrite(output, manifest)
    with pytest.raises(EvidenceError):
        verify_bundle(output)


def test_verifier_still_accepts_evidence_without_the_record(tmp_path, monkeypatch):
    """The Mac's 2.0.38 run and every earlier bundle have no record."""
    source = _run(tmp_path, monkeypatch)
    assert verify_bundle(build_bundle(source, SHA))['status'] == 'integrity_verified'
