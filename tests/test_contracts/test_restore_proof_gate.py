"""Offline restore gate wiring, failure paths and resource ownership; never Docker."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from ops.database_parity import restore_baselines as baselines
from ops.database_parity import restore_proof_support as support
from ops.database_parity import run_post027_restore as runner

ROOT = Path(__file__).resolve().parents[2]
DIGEST = 'sha256:' + 'a' * 64


def table(root, versions=('2.2.0', '2.2.1', '2.3.0')):
    path = root / baselines.TABLE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'first_required_release': '2.2.0', 'releases': [
        {'version': v, 'image_digest': DIGEST, 'schema_era': 57} for v in versions]}))
    ledger = root / baselines.LEDGERS
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({'releases': {v: ['python:057-slot-feed-policy'] for v in versions}}))
    return path


def output(kind):
    return '\n'.join(pattern.replace(r'\-', '-').replace(r'\.', '.').replace(
        '(?:jobs|printers|jobs-printers)', 'jobs-printers') for pattern in support.expected_facts(kind))


def test_offline_root_imports_and_fact_contract():
    result = subprocess.run([sys.executable, '-c',
        'import ops.database_parity.run_post027_restore; import ops.database_parity.post027_restore_native; '
        'import ops.database_parity.post027_coordinator_native; import ops.database_parity.audit_integrity_native'],
        cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT / 'backend') + os.pathsep + str(ROOT)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert baselines.load_baselines()['releases']
    for kind in ('native', 'coordinator'):
        facts = support.validate_facts(output(kind), kind)
        assert facts and facts[-1] == 'post027-' + kind + '-pass'
    assert len(support.expected_facts('native')) == 32  # 31 existing checks plus previous-release backup.


@pytest.mark.parametrize('candidate,expected', [('2.3.0','2.2.1'), ('2.2.1','2.2.0'), ('2.10.0','2.3.0')])
def test_newest_strictly_below_candidate(tmp_path, candidate, expected):
    table(tmp_path)
    assert baselines.select_baseline(candidate, tmp_path)['version'] == expected


def test_missing_new_release_entry_fails_instead_of_falling_back(tmp_path):
    path = table(tmp_path)
    data = json.loads(path.read_text()); data['releases'].pop(1); path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='missing.*2.2.1'):
        baselines.select_baseline('2.3.0', tmp_path)


@pytest.mark.parametrize('value', ['latest', 'sha256:abc', 'sha256:'+'G'*64, None])
def test_malformed_digest_refused(tmp_path, value):
    path = table(tmp_path)
    data = json.loads(path.read_text()); data['releases'][0]['image_digest'] = value; path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='digest'):
        baselines.load_baselines(tmp_path)


def test_pending_receipt_fails_without_fallback(tmp_path):
    path = table(tmp_path)
    data = json.loads(path.read_text()); data['releases'][1].update(image_digest=None, todo='Await receipt'); path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='TODO.*2.2.1'):
        baselines.select_baseline('2.3.0', tmp_path)
    assert baselines.select_baseline('2.2.1', tmp_path)['version'] == '2.2.0'


def test_candidate_does_not_need_its_unpublished_digest(tmp_path):
    path = table(tmp_path)
    data = json.loads(path.read_text()); data['releases'].pop(); path.write_text(json.dumps(data))
    assert baselines.select_baseline('2.3.0', tmp_path)['version'] == '2.2.1'


def test_recorded_releases_have_baselines():
    baselines.check_release_coverage(ROOT, (ROOT / 'VERSION').read_text().strip())


def test_todo_2_2_1_production_receipt_is_recorded():
    # Intentionally failing closed, not skipped/xfail: the spec requires the real
    # production receipt before a 2.3.0 candidate may pass. Never invent its digest.
    rows = baselines.load_baselines()['releases']
    row = next(row for row in rows if row['version'] == '2.2.1')
    baselines.require_record(row)


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    root = tmp_path / 'repo'; root.mkdir()
    table(root)
    (root / 'VERSION').write_text('2.3.0\n')
    for era in ('056','057'):
        path = root / f'backend/core/schema/preserved{era}/manifest.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'source_commit': 'b'*40}))
    monkeypatch.setattr(runner, 'ROOT', root)
    monkeypatch.setenv('POST027_STAGING', str(tmp_path / 'evidence'))
    monkeypatch.setenv('POST027_CONTAINER_PREFIX', 'odin-restore-proof-test-' + tmp_path.name[-20:])
    monkeypatch.delenv('POST027_CURRENT_ONLY', raising=False)
    monkeypatch.delenv('POST027_RUNTIME_REF', raising=False)
    calls = []
    mode = {'failure': None}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w'):
        pass
    def fake(args, **kwargs):
        calls.append(args)
        if args[0] == 'git':
            if 'rev-parse' in args: value = 'b'*40+'\n'
            elif 'archive' in args: value = archive.getvalue()
            else: value = ''
            return SimpleNamespace(returncode=0, stdout=value, stderr='')
        assert args[0] == 'docker', args
        if args[1] == 'pull' and mode['failure'] == 'pull':
            return SimpleNamespace(returncode=1, stdout='', stderr='unavailable')
        if args[1] == 'pull' and mode['failure'] == 'timeout':
            raise subprocess.TimeoutExpired(args, 600)
        if args[1:3] == ('image','inspect') or args[1:3] == ['image','inspect']:
            if mode['failure'] == 'missing-image':
                return SimpleNamespace(returncode=1, stdout='', stderr='missing image')
            value = DIGEST
        elif args[1] == 'cp':
            Path(args[-1]).write_bytes(b'owned archive'); value = ''
        elif args[1] == 'run' and '_baseline' in args[args.index('--name')+1]:
            value = json.dumps({'archive':'/tmp/restore-proof/backup.dump','schema_era':57})
        elif args[1] == 'run' and args[-1].endswith('post027_restore_native.py'):
            if mode['failure'] == 'native-timeout':
                raise subprocess.TimeoutExpired(args, 5400)
            if mode['failure'] == 'deadline': os.kill(os.getpid(), signal.SIGALRM)
            value = output('native')
            if mode['failure'] == 'missing': value = '\n'.join(value.splitlines()[1:])
            if mode['failure'] == 'wrong-last': value += '\npost027-native-unexpected'
            if mode['failure'] == 'signal': os.kill(os.getpid(), signal.SIGTERM)
        elif args[1] == 'run' and args[-1].endswith('post027_coordinator_native.py'):
            value = output('coordinator')
            if mode['failure'] == 'coordinator': value = 'post027-coordinator-pass'
        elif args[1] == 'run' and args[args.index('--name')+1].endswith('_released'):
            value = '{}'
        else: value = ''
        return SimpleNamespace(returncode=0, stdout=value, stderr='')
    monkeypatch.setattr(subprocess, 'run', fake)
    return calls, mode, tmp_path / 'evidence/post027-restore-native.json'


@pytest.mark.parametrize('failure', [None, 'pull', 'timeout', 'missing-image', 'native-timeout', 'deadline', 'missing', 'wrong-last', 'coordinator', 'signal'])
def test_runner_fails_closed_and_writes_receipt(fake_docker, failure):
    calls, mode, path = fake_docker
    mode['failure'] = failure
    assert runner.main() == int(failure is not None)
    receipt = json.loads(path.read_text())
    assert receipt['passed'] is (failure is None)
    assert receipt['status'] == ('pass' if failure is None else 'fail')
    assert receipt['candidate_commit'] == 'b'*40
    assert receipt['baseline']['image_digest'] == DIGEST
    assert receipt['source_copy_removed'] is True
    assert receipt['durations']['total_seconds'] >= 0
    assert not list(path.parent.glob('src-*'))
    # Cleanup discovery runs before and after every proof, including interruptions.
    assert sum(args[0:3] == ('docker', 'container', 'ls') for args in calls) == 2
    if failure is None:
        assert len(receipt['native_facts']) == 32
        assert len(receipt['coordinator_facts']) == 6
        assert len([args for args in calls if args[0:2] == ('docker','pull')]) == 3


@pytest.mark.parametrize('kind', ['container', 'network', 'volume'])
def test_leftover_cleanup_is_label_and_prefix_scoped(monkeypatch, kind):
    calls = []
    def fake(*args, **kwargs):
        calls.append(args)
        if args[1:3] == (kind, 'ls'): return 'odin-restore-proof-old\n'
        return ''
    monkeypatch.setattr(support, 'run', fake)
    support.cleanup('odin-restore-proof-')
    assert all('label='+support.LABEL+'=odin-restore-proof-' in args for args in calls if args[2] == 'ls')
    removed = [args for args in calls if args[2] == 'rm']
    assert len(removed) == 1 and removed[0][-1] == 'odin-restore-proof-old'


def test_cleanup_refuses_a_name_outside_its_prefix(monkeypatch):
    calls = []
    def fake(*args, **kwargs):
        calls.append(args)
        return 'someone-elses-container\n' if args[2] == 'ls' else ''
    monkeypatch.setattr(support, 'run', fake)
    with pytest.raises(RuntimeError, match='unexpected name'):
        support.cleanup('odin-restore-proof-')
    assert not any(args[2] == 'rm' for args in calls)


def test_cleanup_failure_does_not_skip_other_resource_kinds(monkeypatch):
    calls = []
    def fake(*args, **kwargs):
        calls.append(args)
        if args[2] == 'ls': return 'odin-restore-proof-old\n'
        if args[1] == 'container': raise RuntimeError('Removal failed')
        return ''
    monkeypatch.setattr(support, 'run', fake)
    with pytest.raises(RuntimeError, match='cleanup failed'):
        support.cleanup('odin-restore-proof-')
    assert {args[1] for args in calls if args[2] == 'rm'} == {'container','network','volume'}


def test_leftover_is_cleaned_before_work_and_after_failure(monkeypatch, tmp_path):
    prefix = 'odin-restore-proof-recovery-' + tmp_path.name[-16:]
    events = []
    monkeypatch.setattr(support, 'cleanup', lambda scope: events.append(('cleanup', scope)))
    with pytest.raises(RuntimeError, match='work failed'):
        with support.owned_run(prefix):
            events.append(('work', prefix))
            raise RuntimeError('work failed')
    assert events == [('cleanup',prefix), ('work',prefix), ('cleanup',prefix)]


def test_active_prefix_cannot_be_cleaned_by_another_run(monkeypatch, tmp_path):
    prefix = 'odin-restore-proof-lock-' + tmp_path.name[-20:]
    cleaned = []
    monkeypatch.setattr(support, 'cleanup', lambda scope: cleaned.append(scope))
    with support.owned_run(prefix):
        with pytest.raises(RuntimeError, match='Another restore proof'):
            with support.owned_run(prefix):
                pytest.fail('A second run acquired an active prefix')
        assert cleaned == [prefix]
    assert cleaned == [prefix, prefix]


@pytest.mark.parametrize('key,value', [('POST027_CURRENT_ONLY','1'), ('POST027_RUNTIME_REF','other-image')])
def test_required_proof_refuses_scope_and_image_overrides(fake_docker, monkeypatch, key, value):
    calls, mode, path = fake_docker
    monkeypatch.setenv(key, value)
    assert runner.main() == 1
    assert 'cannot skip' in json.loads(path.read_text())['error']
    assert not any(args[0] == 'docker' for args in calls)
