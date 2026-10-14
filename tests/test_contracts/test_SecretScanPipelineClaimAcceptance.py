"""Exercise CI scanner dispatch/fail-closed aggregation without running scanners."""
import json
from pathlib import Path
import pytest

@pytest.mark.parametrize('scanner_fails', [False, True])
def test_security_pipeline_requires_redacted_gitleaks_and_propagates_failure(tmp_path, monkeypatch, scanner_fails):
    from ops.release_control import structured_targets as targets
    from ops.release_control.policy import load_inventory
    inventory = load_inventory()
    component = next(item for item in inventory['components'] if item['id'] == 'CV03')
    assert component['command'] == ['make', 'security-structured', 'PYTHON=python3.11', 'SECURITY_PYTHON=python3.11']
    directory = tmp_path / 'security'
    directory.mkdir()
    monkeypatch.setattr(targets, '_directory', lambda target: directory)
    monkeypatch.setattr(targets, 'ROOT', tmp_path)
    calls = []
    def execute(**kwargs):
        calls.append(kwargs)
        failed = scanner_fails and kwargs['gate_id'] == 'gitleaks'
        result = {'gate_id':kwargs['gate_id'], 'status':'fail' if failed else 'pass',
                  'findings':['command exited 1'] if failed else [],
                  'started_at':'2026-10-12T00:00:00Z', 'ended_at':'2026-10-12T00:00:01Z',
                  'duration_seconds':1, 'timed_out':False,
                  'counts':dict(tests=0, passed=0, failures=0, errors=0, skipped=0, xfailed=0)}
        kwargs['output'].write_text(json.dumps(result))
        return result
    monkeypatch.setattr(targets, 'execute', execute)  # process boundary: NO scanner/HTTP executed
    monkeypatch.setattr(targets, 'render', lambda *args: None)
    assert targets.security() == (1 if scanner_fails else 0)
    scan = next(call for call in calls if call['gate_id'] == 'gitleaks')
    assert scan['command'] == ['gitleaks','detect','--source','.','--config','.gitleaks.toml',
        '--redact=100','--report-format','json','--report-path',str(directory/'gitleaks.json')]
    assert scan['expected_artifacts'] == ['security/gitleaks.json']
    assert scan['timeout'] == 900
    aggregate = json.loads((directory/'result.json').read_text())
    assert aggregate['status'] == ('fail' if scanner_fails else 'pass')
    assert aggregate['findings'] == (['gitleaks: command exited 1'] if scanner_fails else [])
