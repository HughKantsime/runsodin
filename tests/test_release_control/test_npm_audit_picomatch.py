"""Acceptance regressions using Dev's recorded full and production npm audits."""
from __future__ import annotations

import copy
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.release_control import npm_audit_policy as policy

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)
ADVISORY = 'GHSA-fc5q-39xc-vjpf'


@pytest.fixture
def npm_reports():
    fixtures = Path(__file__).parent / 'fixtures'
    return tuple(json.loads((fixtures / f'npm-audit-picomatch-{scope}.json').read_bytes())
                 for scope in ('full', 'production'))


@pytest.fixture
def approved(monkeypatch):
    records = copy.deepcopy(policy.ACCEPTANCES)
    monkeypatch.setattr(policy, 'ACCEPTANCES', records)
    acceptance = next(record for record in records if record['advisory'] == ADVISORY)
    monkeypatch.setitem(acceptance, 'approved_by', 'Shane')
    monkeypatch.setitem(acceptance, 'approved_on', '2026-11-17')
    return acceptance


@pytest.fixture
def unapproved(monkeypatch):
    """The record as it was before the owner approved: both fields empty (a copy, never the file)."""
    records = copy.deepcopy(policy.ACCEPTANCES)
    monkeypatch.setattr(policy, 'ACCEPTANCES', records)
    acceptance = next(record for record in records if record['advisory'] == ADVISORY)
    monkeypatch.setitem(acceptance, 'approved_by', '')
    monkeypatch.setitem(acceptance, 'approved_on', '')
    return acceptance


def assess(reports, **kwargs):
    full, production = reports
    return policy.assess(full, 1, now=kwargs.pop('now', NOW), production_report=production,
                         production_exit_code=0, **kwargs)


def test_committed_policy_records_the_owner_approval_and_expiry():
    # Shane approved on 2026-11-17 (ODIN Dev chat). A change to any of these needs a new approval.
    record = next(record for record in policy.ACCEPTANCES if record['advisory'] == ADVISORY)
    assert (record['approved_by'], record['approved_on']) == ('Shane', '2026-11-17')
    assert record['expires_at'] == '2026-12-17T00:00:00Z'


def test_acceptance_matches_recorded_graph_and_lockfile(npm_reports):
    record = next(record for record in policy.ACCEPTANCES if record['advisory'] == ADVISORY)
    full = npm_reports[0]
    vulnerabilities = full['vulnerabilities']
    lock = json.loads((policy.ROOT / 'frontend/package-lock.json').read_bytes())
    assert record['source'] == vulnerabilities['picomatch']['via'][0]['source'] == 1247710
    assert record['coverage'] == full['metadata']['dependencies']
    assert record['edges'] == {name: entry['via'] for name, entry in vulnerabilities.items()
                               if name != 'picomatch'}
    assert record['direct'] == {name for name, entry in vulnerabilities.items() if entry['isDirect']}
    assert record['versions'] == {path: lock['packages'][path]['version']
                                  for entry in vulnerabilities.values() for path in entry['nodes']}
    assert all(lock['packages'][path]['dev'] is True for path in record['versions'])


@pytest.mark.parametrize('field', ['approved_by', 'approved_on'])
@pytest.mark.parametrize('empty', ['', ' ', None])
def test_owner_lock_either_empty_field_fails(npm_reports, approved, monkeypatch, field, empty):
    monkeypatch.setitem(approved, field, empty)
    with pytest.raises(policy.AuditError, match=f'Build-only acceptance for {ADVISORY} is not approved by the owner'):
        assess(npm_reports)


def test_owner_lock_both_filled_passes(npm_reports, approved):
    assert assess(npm_reports) == 'pass_with_exception'


def test_expiry_is_pinned(approved):
    assert approved['expires_at'] == '2026-12-17T00:00:00Z'


@pytest.mark.parametrize('when', ['2026-12-16T23:59:59+00:00', '2026-12-17T00:00:00+00:00', '2026-12-18T00:00:00+00:00'])
def test_expiry_boundary(npm_reports, approved, when):
    now = datetime.fromisoformat(when)
    if now.day == 16:
        assert assess(npm_reports, now=now) == 'pass_with_exception'
    else:
        with pytest.raises(policy.AuditError, match='Build-only acceptance expired'):
            assess(npm_reports, now=now)


@pytest.mark.parametrize('field,value', [('url', 'https://github.com/advisories/OTHER'),
    ('name', 'other'), ('dependency', 'other'), ('range', '<4.0.7'), ('severity', 'moderate'),
    ('source', 0.0), ('source', 0), ('source', 1247711), ('source', 1247710.0)])
def test_advisory_identity_is_exact(npm_reports, approved, field, value):
    npm_reports[0]['vulnerabilities']['picomatch']['via'][0][field] = value
    with pytest.raises(policy.AuditError, match='Unapproved advisory'):
        assess(npm_reports)


@pytest.mark.parametrize('change', ['second_advisory', 'extra_package', 'missing_package', 'edge', 'node', 'direct', 'critical', 'coverage'])
def test_unapproved_graph_and_exposure_fail(npm_reports, approved, change):
    full = npm_reports[0]
    vulns = full['vulnerabilities']
    if change == 'second_advisory': vulns['picomatch']['via'].append(copy.deepcopy(vulns['picomatch']['via'][0]))
    elif change == 'extra_package':
        vulns['other'] = dict(vulns['vite'], name='other')
        full['metadata']['vulnerabilities'].update(high=8, total=8)
    elif change == 'missing_package':
        del vulns['fdir']
        full['metadata']['vulnerabilities'].update(high=6, total=6)
    elif change == 'edge': vulns['vite']['via'] = ['picomatch']
    elif change == 'node': vulns['picomatch']['nodes'].pop()
    elif change == 'direct': vulns['fdir']['isDirect'] = True
    elif change == 'critical':
        vulns['picomatch']['severity'] = 'critical'
        full['metadata']['vulnerabilities'].update(high=6, critical=1)
    else: full['metadata']['dependencies']['total'] += 1
    with pytest.raises(policy.AuditError): assess(npm_reports)


@pytest.mark.parametrize('filename', ['frontend/package-lock.json', 'Dockerfile'])
def test_current_build_input_hash_is_pinned(npm_reports, approved, tmp_path, filename):
    for name in approved['file_hashes']:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((policy.ROOT / name).read_bytes())
    with (tmp_path / filename).open('ab') as stream: stream.write(b'\n')
    with pytest.raises(policy.AuditError, match='Reviewed build input changed: ' + filename):
        assess(npm_reports, root=tmp_path)


@pytest.mark.parametrize('path', [
    'node_modules/@vitejs/plugin-react', 'node_modules/rollup-plugin-visualizer',
    'node_modules/rollup-plugin-visualizer/node_modules/picomatch', 'node_modules/tinyglobby',
    'node_modules/tinyglobby/node_modules/fdir', 'node_modules/tinyglobby/node_modules/picomatch',
    'node_modules/vite', 'node_modules/vite/node_modules/picomatch',
    'node_modules/vitest', 'node_modules/vitest/node_modules/picomatch'])
@pytest.mark.parametrize('change', ['production', 'version'])
def test_every_affected_node_is_exact_and_dev_only(approved, monkeypatch, tmp_path, path, change):
    for name in approved['file_hashes']:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((policy.ROOT / name).read_bytes())
    lock_path = tmp_path / 'frontend/package-lock.json'
    lock = json.loads(lock_path.read_bytes())
    lock['packages'][path]['dev' if change == 'production' else 'version'] = False if change == 'production' else '0.0.0'
    lock_path.write_text(json.dumps(lock))
    monkeypatch.setitem(approved['file_hashes'], 'frontend/package-lock.json', policy.digest(lock_path.read_bytes()))
    with pytest.raises(policy.AuditError, match='approved development-only version'):
        policy.check_context(tmp_path, approved)


def test_missing_production_proof_fails(npm_reports, approved):
    with pytest.raises(policy.AuditError, match='Missing production-only audit proof'):
        policy.assess(npm_reports[0], 1, now=NOW)


@pytest.mark.parametrize('change', ['high', 'critical', 'moderate', 'counts', 'coverage', 'version', 'error', 'status'])
def test_invalid_or_vulnerable_production_proof_fails(npm_reports, approved, change):
    production = npm_reports[1]
    status = 0
    if change in ('high', 'critical', 'moderate'):
        production['vulnerabilities']['other'] = {'name': 'other', 'severity': change}
        production['metadata']['vulnerabilities'].update({change: 1, 'total': 1})
        status = int(change != 'moderate')
    elif change == 'counts': production['metadata']['vulnerabilities']['total'] = 1
    elif change == 'coverage': production['metadata']['dependencies']['dev'] = 0
    elif change == 'version': production['auditReportVersion'] = 1
    elif change == 'error': production['error'] = {}
    else: status = 2
    with pytest.raises(policy.AuditError):
        policy.assess(npm_reports[0], 1, now=NOW, production_report=production, production_exit_code=status)


def mock_audits(monkeypatch, reports, second_failure=None):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs['env'].keys() <= {'PATH', 'SYSTEMROOT', 'HOME'}
        assert kwargs['env']['HOME'] == str(kwargs['cwd'])
        assert '--registry=https://registry.npmjs.org' in command
        assert '--userconfig=' + str(kwargs['cwd'] / 'user.npmrc') in command
        assert '--globalconfig=' + str(kwargs['cwd'] / 'global.npmrc') in command
        for name in ('package.json', 'package-lock.json'):
            assert (kwargs['cwd'] / name).read_bytes() == (policy.ROOT / 'frontend' / name).read_bytes()
        if len(calls) == 1:
            assert '--include=dev' in command and '--omit=dev' not in command
            return SimpleNamespace(stdout=json.dumps(reports[0]).encode(), returncode=1)
        assert '--omit=dev' in command and '--include=dev' not in command
        assert all('--include=' + flag in command for flag in ('optional', 'peer'))
        if second_failure == 'timeout': raise subprocess.TimeoutExpired('npm', 180)
        if second_failure == 'error': return SimpleNamespace(stdout=b'{"error":{"message":"untrusted details"}}', returncode=1)
        if second_failure == 'json': return SimpleNamespace(stdout=b'{', returncode=1)
        return SimpleNamespace(stdout=json.dumps(reports[1]).encode(), returncode=0)
    monkeypatch.setenv('NPM_CONFIG_INCLUDE', 'dev')
    monkeypatch.setenv('NODE_OPTIONS', '--bad')
    monkeypatch.setattr(policy.subprocess, 'run', run)
    original = policy.assess
    monkeypatch.setattr(policy, 'assess', lambda *args, **kwargs: original(*args, now=NOW, **kwargs))
    return calls


def test_two_isolated_runs_and_decision_evidence(npm_reports, approved, monkeypatch, tmp_path):
    calls = mock_audits(monkeypatch, npm_reports)
    output, decision = tmp_path / 'audit.json', tmp_path / 'decision.json'
    assert policy.audit(output, decision) == 0
    assert len(calls) == 2
    saved = json.loads(decision.read_bytes())
    assert saved['status'] == 'pass_with_exception'
    assert saved['advisory'] == ADVISORY
    assert saved['expires_at'] == '2026-12-17T00:00:00Z'
    assert saved['approved_by'] == 'Shane' and saved['approved_on'] == '2026-11-17'
    assert saved['production_only_counts'] == dict.fromkeys(['info', 'low', 'moderate', 'high', 'critical', 'total'], 0)
    assert saved['policy_sha256'] == policy.digest(policy.Path(policy.__file__).read_bytes())
    assert saved['raw_audit_sha256'] == policy.digest(output.read_bytes())


@pytest.mark.parametrize('failure', ['timeout', 'error', 'json'])
def test_second_audit_failure_discards_success(npm_reports, approved, monkeypatch, tmp_path, failure):
    calls = mock_audits(monkeypatch, npm_reports, second_failure=failure)
    output, decision = tmp_path / 'audit.json', tmp_path / 'decision.json'
    decision.write_text('{"status":"pass"}')
    assert policy.audit(output, decision) == 1
    assert len(calls) == 2
    assert json.loads(decision.read_bytes())['status'] == 'fail'
    assert output.read_text().strip() == '{}'
    assert 'untrusted details' not in decision.read_text()


def test_unapproved_gate_records_owner_refusal(npm_reports, unapproved, monkeypatch, tmp_path):
    calls = mock_audits(monkeypatch, npm_reports)
    output, decision = tmp_path / 'audit.json', tmp_path / 'decision.json'
    assert policy.audit(output, decision) == 1
    assert len(calls) == 1
    assert json.loads(decision.read_bytes())['reason'] == f'Build-only acceptance for {ADVISORY} is not approved by the owner'


def test_mismatched_graph_precedes_owner_refusal(npm_reports, unapproved, monkeypatch, tmp_path):
    npm_reports[0]['vulnerabilities']['vitest']['via'].append('vite')
    calls = mock_audits(monkeypatch, npm_reports)
    output, decision = tmp_path / 'audit.json', tmp_path / 'decision.json'
    assert policy.audit(output, decision) == 1
    assert len(calls) == 1
    assert json.loads(decision.read_bytes())['reason'] == 'Unapproved vulnerability graph'
