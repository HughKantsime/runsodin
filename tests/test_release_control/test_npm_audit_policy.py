from __future__ import annotations
import copy
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.release_control import npm_audit_policy as policy

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def report():
    vulnerabilities = {
        name: {'name': name, 'severity': 'high', 'isDirect': name == 'tailwindcss',
               'nodes': ['node_modules/' + name], 'via': list(policy.EDGES.get(name, []))}
        for name in policy.VERSIONS
    }
    vulnerabilities['braces']['via'] = [{'source': 1240992, 'name': 'braces', 'dependency': 'braces',
        'url': 'https://github.com/advisories/GHSA-vfj7-8cjw-p6xm', 'severity': 'high', 'range': '<=3.0.3'}]
    return {'auditReportVersion': 2, 'vulnerabilities': vulnerabilities,
            'metadata': {'dependencies': dict(policy.COVERAGE), 'vulnerabilities': {
                'info': 0, 'low': 0, 'moderate': 0, 'high': 5, 'critical': 0, 'total': 5}}}


def clean_report():
    value = report()
    value['vulnerabilities'] = {}
    value['metadata']['vulnerabilities'] = dict.fromkeys(value['metadata']['vulnerabilities'], 0)
    return value


def test_exact_report_and_real_context():
    assert policy.assess(report(), 1, now=NOW) == 'pass_with_exception'


def test_clean_report_needs_no_exception_after_expiry():
    assert policy.assess(clean_report(), 0, now=datetime(2027, 1, 1, tzinfo=timezone.utc)) == 'pass'


@pytest.mark.parametrize('change', ['extra', 'critical', 'cycle', 'empty', 'missing', 'advisory',
                                   'nodes', 'direct', 'counts', 'coverage', 'version', 'error', 'float'])
def test_changed_report_fails(change):
    value = report()
    entry = value['vulnerabilities']['braces']
    if change == 'extra': value['vulnerabilities']['other'] = copy.deepcopy(entry)
    elif change == 'critical': entry['severity'] = 'critical'
    elif change == 'cycle': value['vulnerabilities']['chokidar']['via'] = ['tailwindcss']
    elif change == 'empty': entry['via'] = []
    elif change == 'missing': del value['vulnerabilities']['micromatch']
    elif change == 'advisory': entry['via'][0]['url'] = 'https://github.com/advisories/OTHER'
    elif change == 'nodes': entry['nodes'] += ['node_modules/other/node_modules/braces']
    elif change == 'direct': entry['isDirect'] = True
    elif change == 'counts': value['metadata']['vulnerabilities']['high'] = 0
    elif change == 'coverage': value['metadata']['dependencies']['dev'] = 0
    elif change == 'version': value['auditReportVersion'] = 1
    elif change == 'error': value['error'] = {}
    elif change == 'float': value['metadata']['dependencies']['prod'] = 64.0
    with pytest.raises(policy.AuditError): policy.assess(value, 1, now=NOW)


@pytest.mark.parametrize('status', [0, 2, -1, True])
def test_exit_mismatch(status):
    with pytest.raises(policy.AuditError): policy.assess(report(), status, now=NOW)


def test_expiry():
    with pytest.raises(policy.AuditError, match='expired'):
        policy.assess(report(), 1, now=datetime(2026, 10, 19, tzinfo=timezone.utc))


@pytest.mark.parametrize('raw', [b'', b'[]', b'{', b'{"a":1,"a":2}', b'{"value":NaN}'])
def test_invalid_json(raw):
    with pytest.raises(policy.AuditError):
        policy.assess(policy.strict_json(raw), 1, now=NOW)


@pytest.mark.parametrize('kind', ['version', 'docker', 'lock', 'frontend', 'production'])
def test_context_drift(tmp_path, monkeypatch, kind):
    (tmp_path / 'frontend').mkdir()
    (tmp_path / 'VERSION').write_text('1.9.17')
    for name in policy.FILE_HASHES:
        (tmp_path / name).write_bytes((policy.ROOT / name).read_bytes())
    monkeypatch.setattr(policy, 'frontend_digest', lambda root: policy.FRONTEND_HASH)
    if kind == 'version': (tmp_path / 'VERSION').write_text('1.9.18')
    elif kind == 'docker': (tmp_path / 'Dockerfile').write_text('changed')
    elif kind == 'lock': (tmp_path / 'frontend/package-lock.json').write_text('{}')
    elif kind == 'frontend': monkeypatch.setattr(policy, 'frontend_digest', lambda root: 'changed')
    else:
        path = tmp_path / 'frontend/package-lock.json'
        value = json.loads(path.read_bytes()); value['packages']['node_modules/braces']['dev'] = False
        path.write_text(json.dumps(value))
        monkeypatch.setitem(policy.FILE_HASHES, 'frontend/package-lock.json', policy.digest(path.read_bytes()))
    with pytest.raises(policy.AuditError): policy.check_context(tmp_path)


def test_isolated_full_audit_and_retained_evidence(tmp_path, monkeypatch):
    raw = json.dumps(report()).encode()
    def run(command, **kwargs):
        assert all('--include=' + value in command for value in ('dev', 'optional', 'peer'))
        assert '--registry=https://registry.npmjs.org' in command
        assert 'NPM_CONFIG_OMIT' not in kwargs['env'] and 'NODE_OPTIONS' not in kwargs['env']
        assert not (kwargs['cwd'] / '.npmrc').exists()
        return SimpleNamespace(stdout=raw, returncode=1)
    monkeypatch.setenv('NPM_CONFIG_OMIT', 'dev'); monkeypatch.setenv('NODE_OPTIONS', '--bad')
    monkeypatch.setattr(policy.subprocess, 'run', run)
    monkeypatch.setattr(policy, 'check_context', lambda root: None)
    original = policy.assess
    monkeypatch.setattr(policy, 'assess', lambda value, status, **kwargs: original(value, status, now=NOW))
    output, decision = tmp_path / 'raw.json', tmp_path / 'decision.json'
    assert policy.audit(output, decision) == 0
    assert output.read_bytes() == raw
    saved = json.loads(decision.read_bytes())
    assert saved['status'] == 'pass_with_exception' and saved['npm_exit_code'] == 1
    assert saved['raw_audit_sha256'] == policy.digest(raw)


def test_timeout_overwrites_prior_success(tmp_path, monkeypatch):
    def run(*args, **kwargs): raise subprocess.TimeoutExpired('npm', 180)
    monkeypatch.setattr(policy.subprocess, 'run', run)
    output, decision = tmp_path / 'raw.json', tmp_path / 'decision.json'
    decision.write_text('{"status":"pass"}')
    assert policy.audit(output, decision) == 1
    assert json.loads(decision.read_bytes())['status'] == 'fail'


def test_unknown_audit_payload_is_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(policy.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(stdout=b'{"message":"untrusted details"}', returncode=2))
    output, decision = tmp_path / 'raw.json', tmp_path / 'decision.json'
    assert policy.audit(output, decision) == 1
    assert 'untrusted details' not in output.read_text() + decision.read_text()


def test_both_gate_entrypoints_use_policy():
    root = policy.ROOT
    assert 'ops.release_control.npm_audit_policy' in (root / 'Makefile').read_text()
    structured = (root / 'ops/release_control/structured_targets.py').read_text()
    assert 'ops.release_control.npm_audit_policy' in structured
    assert "expected.append(str((directory / 'npm-audit-decision.json')" in structured
    import shlex
    command = next(line for line in (root / 'Makefile').read_text().splitlines()
                   if 'ops.release_control.npm_audit_policy' in line)
    args = shlex.split(command)
    for flag in ('--output', '--decision'):
        target = args[args.index(flag) + 1]
        ignored = subprocess.run(['git', 'check-ignore', target], cwd=root, capture_output=True)
        assert ignored.returncode == 0, 'Audit artifacts must not dirty the candidate checkout'


def test_ignored_source_and_public_assets_change_fingerprint(tmp_path):
    frontend = tmp_path / 'frontend'
    (frontend / 'src').mkdir(parents=True)
    (frontend / 'public').mkdir()
    initial = policy.frontend_digest(tmp_path)
    (frontend / 'src/widget.bak.ts').write_text('unreviewed source')
    assert policy.frontend_digest(tmp_path) != initial
    previous = policy.frontend_digest(tmp_path)
    (frontend / 'public/vendor.bak.js').write_text('unreviewed public asset')
    assert policy.frontend_digest(tmp_path) != previous


def test_noninteger_advisory_source_is_rejected():
    value = report()
    value['vulnerabilities']['braces']['via'][0]['source'] = 1240992.0
    with pytest.raises(policy.AuditError): policy.assess(value, 1, now=NOW)
