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
    assert len(support.expected_facts('native')) == 34  # Includes the released 062 restore and previous-release backup.


def test_native_062_routing_switch_matches_other_released_ledgers():
    from inspect import getsource
    from ops.database_parity import post027_restore_native

    # This is an offline wiring check. The native proof exercises the selected route.
    source = getsource(post027_restore_native.main)
    assert 'if ledger062 >= set(post_ids) else nullcontext()' in source


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
    # One repository coverage test owns the pending production receipt failure.
    row = next(row for row in baselines.load_baselines()['releases'] if row['version'] == '2.2.1')
    baselines.require_record(row)


def test_todo_2_2_1_production_receipt_is_recorded():
    # Diagnostic regression; the repository coverage test above owns the failure.
    with pytest.raises(ValueError) as error:
        baselines.require_record({'version': '2.2.1', 'image_digest': None})
    message = str(error.value)
    for required in ('version=2.2.1', 'image_digest=target_digest', 'successful production receipt',
                     'schema_era', 'ops/database_parity/restore_baselines.json'):
        assert required in message


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    root = tmp_path / 'repo'; root.mkdir()
    table(root)
    (root / 'VERSION').write_text('2.3.0\n')
    for era in ('056','057','060','062'):
        path = root / f'backend/core/schema/preserved{era}/manifest.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'source_commit': 'b'*40}))
    monkeypatch.setattr(runner, 'ROOT', root)
    monkeypatch.setenv('POST027_STAGING', str(tmp_path / 'evidence'))
    monkeypatch.setenv('POST027_CONTAINER_PREFIX', 'odin-restore-proof-test-' + tmp_path.name[-20:])
    monkeypatch.delenv('POST027_CURRENT_ONLY', raising=False)
    monkeypatch.delenv('POST027_RUNTIME_REF', raising=False)
    monkeypatch.setenv('POST027_CANDIDATE_IMAGE', 'sha256:' + 'c' * 64)
    monkeypatch.setattr(support, 'state_directory', lambda: tmp_path / 'private-state', raising=False)
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
            elif 'status' in args: value = mode.get('dirty', '')
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
            value = ('sha256:' + 'c' * 64) if args[-1] == 'sha256:' + 'c' * 64 else DIGEST
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
        if args[1] == 'run' and args[-1].endswith(('post027_restore_native.py', 'post027_coordinator_native.py')):
            value += mode.get('extra_output', '')
        return SimpleNamespace(returncode=0, stdout=value, stderr=mode.get('stderr', ''))
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
        assert len(receipt['native_facts']) == 34
        assert receipt['released060_commit'] == 'b'*40
        assert any('POST027_RELEASED060_SOURCE=/source/released060/backend' in args for args in calls)
        assert receipt['released062_commit'] == 'b'*40
        assert any('POST027_RELEASED062_SOURCE=/source/released062/backend' in args for args in calls)
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


@pytest.fixture(autouse=True)
def private_proof_state(tmp_path, monkeypatch):
    monkeypatch.setattr(support, 'state_directory', lambda: tmp_path / 'private-state', raising=False)


def test_restore_uses_candidate_image_and_previous_release_only_for_backup(fake_docker):
    calls, _, path = fake_docker
    assert runner.main() == 0
    for args in calls:
        if args[:2] == ['docker', 'run'] and args[-1].endswith(('post027_restore_native.py', 'post027_coordinator_native.py')):
            assert 'sha256:' + 'c' * 64 in args
            assert DIGEST not in args
    assert json.loads(path.read_text())['candidate_image'] == 'sha256:' + 'c' * 64


def test_no_candidate_image_fails_closed(fake_docker, monkeypatch):
    calls, _, path = fake_docker
    monkeypatch.delenv('POST027_CANDIDATE_IMAGE')
    assert runner.main() == 1
    assert 'candidate image' in json.loads(path.read_text())['error'].lower()
    assert not any(args[:2] == ('docker', 'pull') for args in calls)


def test_child_logs_use_existing_redaction(fake_docker):
    _, mode, path = fake_docker
    mode['extra_output'] = '\nJWT_SECRET_KEY=synthetic-regression-value'
    mode['stderr'] = 'Authorization: Bearer synthetic-regression-token'
    assert runner.main() == 0
    for log in path.parent.glob('*.log'):
        assert 'synthetic-regression' not in log.read_text()
        assert '[REDACTED]' in log.read_text()
    assert len(list(path.parent.glob('*.log'))) == 2


def test_source_and_dump_are_outside_evidence(fake_docker):
    calls, _, path = fake_docker
    assert runner.main() == 0
    copied = next(Path(args[-1]) for args in calls if args[:2] == ('docker', 'cp'))
    assert not copied.is_relative_to(path.parent)
    assert not copied.parent.exists()


@pytest.mark.parametrize('dirty', [' M backend/main.py\0', '?? ops/uncommitted.py\0', ' M frontend/src/App.tsx\0'])
def test_dirty_candidate_fails_closed(fake_docker, dirty):
    calls, mode, path = fake_docker
    mode['dirty'] = dirty
    assert runner.main() == 1
    assert 'dirty' in json.loads(path.read_text())['error'].lower()
    assert not any(args[0] == 'docker' for args in calls)


@pytest.mark.parametrize('count', [1, 7])
def test_dirty_refusal_reports_bounded_paths(fake_docker, capsys, count):
    calls, mode, path = fake_docker
    paths = [f'ops/changed {index}.py' for index in range(count)]
    mode['dirty'] = ''.join(' M ' + name + '\0' for name in paths)
    assert runner.main() == 1
    receipt = json.loads(path.read_text())
    assert receipt['source_dirty'] is True
    assert receipt['source_dirty_paths'] == paths[:5]
    message = capsys.readouterr().out
    for name in paths[:5]:
        assert name in receipt['error'] and name in message
    for name in paths[5:]:
        assert name not in json.dumps(receipt) and name not in message
    assert not any(args[0] == 'docker' for args in calls)


def test_dirty_paths_handle_renames_and_unusual_names(tmp_path, monkeypatch, capsys):
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True)
    git('init', '-q')
    old = 'old name.py'
    new = 'new\nname.py'
    content = 'file contents must not appear in diagnostics'
    (tmp_path / old).write_text(content)
    git('add', old)
    git('-c', 'user.name=Offline test', '-c', 'user.email=offline@example.invalid',
        '-c', 'core.hooksPath=/dev/null', 'commit', '--no-gpg-sign', '-qm', 'fixture')
    git('mv', old, new)
    (tmp_path / 'untracked file.py').write_text(content)
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setenv('POST027_STAGING', str(tmp_path.parent / 'evidence'))
    assert runner.main() == 1
    receipt = json.loads((tmp_path.parent / 'evidence/post027-restore-native.json').read_text())
    assert receipt['source_dirty_paths'] == [new, old, 'untracked file.py']
    message = capsys.readouterr().out
    assert json.dumps(new) in message and json.dumps(old) in message
    assert content not in message and content not in json.dumps(receipt)


def test_generated_gate_fixture_is_excluded(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    fixture = tmp_path / 'frontend/src/test-fixtures/cert-C-models-wire.json'
    fixture.parent.mkdir(parents=True); fixture.write_text('{}')
    assert not runner.source_is_dirty(tmp_path)
    other = fixture.with_name('unrecognized.json'); other.write_text('{}')
    assert runner.source_is_dirty(tmp_path)
    other.unlink()
    static = fixture.with_name('job-completion-wire.json'); static.write_text('{}')
    assert runner.source_is_dirty(tmp_path)


def test_historical_image_is_pinned_in_baseline_table():
    row = next(row for row in baselines.load_baselines()['releases'] if row['version'] == '1.9.24')
    recorded = json.loads((ROOT / 'backend/core/schema/orm_baselines.json').read_text())['published_1_9_13_to_1_9_24']['image']
    assert row['image_digest'] == recorded.split('@')[1]
    assert runner.RELEASED_IMAGE.endswith('@' + row['image_digest'])


def test_lock_refuses_symlink(monkeypatch, tmp_path):
    import hashlib
    prefix = 'odin-restore-proof-symlink'
    state = support.state_directory(); state.mkdir(mode=0o700)
    target = tmp_path / 'target'; target.write_text('untouched')
    (state / (hashlib.sha256(prefix.encode()).hexdigest() + '.lock')).symlink_to(target)
    monkeypatch.setattr(support, 'cleanup', lambda _: None)
    with pytest.raises(OSError):
        with support.owned_run(prefix):
            pytest.fail('followed lock symlink')
    assert target.read_text() == 'untouched'


def test_stale_staging_is_cleaned_at_next_start(monkeypatch):
    monkeypatch.setattr(support, 'cleanup', lambda _: None)
    prefix = 'odin-restore-proof-stale'
    with support.owned_run(prefix) as source:
        assert isinstance(source, Path)
        stale = source
    stale.mkdir(parents=True)
    (stale / 'baseline.dump').write_bytes(b'prior killed run')
    with support.owned_run(prefix) as source:
        assert source == stale
        assert not source.exists()
    assert not stale.exists()


def test_initial_failure_receipt_precedes_work(fake_docker, monkeypatch):
    _, _, path = fake_docker
    observed = []
    def interrupted(*args):
        observed.append(json.loads(path.read_text()))
        raise KeyboardInterrupt
    monkeypatch.setattr(runner, 'prove', interrupted)
    assert runner.main() == 1
    assert observed and observed[0]['status'] == 'fail' and observed[0]['passed'] is False
    assert json.loads(path.read_text())['status'] == 'fail'


def test_proof_kill_leaves_failure_receipt(tmp_path):
    code = """
import contextlib, os, signal
from pathlib import Path
from ops.database_parity import run_post027_restore as r
r.run = lambda *a, **kw: 'b' * 40 if 'rev-parse' in a else ''
r.select_baseline = lambda *a: {}
r.owned_run = lambda *a: contextlib.nullcontext(Path('/unused-source'))
r.prove = lambda *a: os.kill(os.getpid(), signal.SIGKILL)
r.main()
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                            env={**os.environ, 'POST027_STAGING': str(tmp_path)}, capture_output=True)
    assert result.returncode == -signal.SIGKILL
    receipt = json.loads((tmp_path / 'post027-restore-native.json').read_text())
    assert receipt['status'] == 'fail' and receipt['passed'] is False


def test_cv05_budget_covers_parity_proof_and_cleanup():
    inventory = json.loads((ROOT / 'ops/release_control/inventory.json').read_text())
    cv05 = next(row for row in inventory['components'] if row['id'] == 'CV05')
    assert cv05['timeout_seconds'] >= 3600 + support.PROOF_TIMEOUT + support.CLEANUP_ALLOWANCE


def test_baseline_embedded_script_imports_and_signatures():
    import ast
    import hashlib
    import importlib
    import inspect
    import sqlalchemy
    from sqlalchemy.engine import Engine, Connection
    tree = ast.parse(runner.BASELINE_BACKUP_SOURCE)
    compile(tree, '<baseline-backup>', 'exec')
    # Resolve the preserved 057 inventory without importing current product code.
    # Each selected file must match the preserved manifest's hash.
    manifest = json.loads((ROOT / 'backend/core/schema/preserved057/manifest.json').read_text())
    def preserved(relative):
        for path in (ROOT / 'backend/core/schema').glob('preserved*/**/' + relative):
            if hashlib.sha256(path.read_bytes()).hexdigest() == manifest['sources'][relative]:
                return ast.parse(path.read_text())
        pytest.fail('Missing preserved source: ' + relative)

    def exported(module, name):
        relative = module.replace('.', '/')
        relative += '/__init__.py' if module == 'core.schema' else '.py'
        source = preserved(relative)
        for node in source.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                args = node.args
                for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                    arg.annotation = None
                args.defaults = [ast.Constant(None) for _ in args.defaults]
                args.kw_defaults = [ast.Constant(None) if v is not None else None for v in args.kw_defaults]
                definition = ast.FunctionDef(name=name, args=args, body=[ast.Pass()], decorator_list=[])
                namespace = {}
                exec(compile(ast.fix_missing_locations(ast.Module(body=[definition], type_ignores=[])), '<signature>', 'exec'), namespace)
                return namespace[name]
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if (alias.asname or alias.name) == name:
                        return exported(node.module, alias.name)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                    return object()
        pytest.fail(f'Missing preserved export: {module}.{name}')

    imports = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name] = importlib.import_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports[alias.asname or alias.name] = (exported(node.module, alias.name)
                    if node.module.startswith(('core', 'modules')) else getattr(importlib.import_module(node.module), alias.name))
    signatures = {
        'root.mkdir': inspect.signature(Path('.').mkdir),
        'path.write_text': inspect.signature(Path('.').write_text),
        'path.chmod': inspect.signature(Path('.').chmod),
        'engine.dispose': inspect.signature(Engine.dispose),
        'engine.begin': inspect.signature(Engine.begin),
        'c.execute': inspect.signature(Connection.execute),
        'str': inspect.signature(lambda value: None),
        'int': inspect.signature(lambda value: None),
        'os.environ.update': inspect.signature(lambda **kwargs: None),
    }
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        name = ast.unparse(call.func)
        if name in signatures:
            signature = signatures[name]
        elif name.endswith('.split'):
            assert name.startswith('POST027_MIGRATIONS[-1][0].split')
            signature = inspect.signature(''.split)
        else:
            parts = name.split('.')
            target = imports.get(parts[0], print if parts[0] == 'print' else None)
            assert target is not None, name
            for part in parts[1:]:
                target = getattr(target, part)
            signature = inspect.signature(target)
        args = [None] * len(call.args)
        if name in ('engine.dispose', 'engine.begin', 'c.execute'):
            args.insert(0, None)
        signature.bind(*args, **{kw.arg: None for kw in call.keywords})


@pytest.mark.parametrize('over_budget', [False, True])
def test_parity_hands_built_image_to_proof_before_cleanup(tmp_path, monkeypatch, over_budget):
    from ops.database_parity import runner as parity
    calls = []
    clock = [0]
    monkeypatch.setattr(parity.time, 'monotonic', lambda: clock[0])
    image = 'sha256:' + 'c' * 64
    secret_file = tmp_path / 'owned-input'; secret_file.touch()
    monkeypatch.setattr(parity, '_generate_parity_secrets', lambda: {})
    monkeypatch.setattr(parity, '_write_secret_bundle', lambda _: secret_file)
    lifecycle = SimpleNamespace(
        begin=lambda: None, mark_build_attempted=lambda: None,
        docker_build_owner_args=lambda: [], establish_final_image=lambda _: None,
        freeze_ownership=lambda: None, finalize=lambda: calls.append('image-cleanup') or [],
        evidence=lambda: {}, ownership_capture_attempted=True, final_image_id=image)
    monkeypatch.setattr(parity, 'DisposableImageLifecycle', lambda *a, **k: lifecycle)
    monkeypatch.setattr(parity, '_docker_resources', lambda _: set())
    monkeypatch.setattr(parity, 'read_iidfile', lambda _: image)
    monkeypatch.setattr(parity, 'verify_built_image', lambda *a, **k: image)
    monkeypatch.setattr(parity, '_assert_image_metadata', lambda _: image)
    monkeypatch.setattr(parity, '_run_sqlite_exact_image', lambda *a: ('ok', {}))
    monkeypatch.setattr(parity, 'inspect_junit', lambda _: {'passed': 140})
    monkeypatch.setattr(parity, '_extract_database_evidence', lambda _: {})
    monkeypatch.setattr(parity, '_extract_topology_evidence', lambda *a: {})
    monkeypatch.setattr(parity, '_sanitize_and_assert_artifacts', lambda *a: None)
    monkeypatch.setattr(parity, 'render_report', lambda *a: None)
    proof_dir = tmp_path / 'restore-proof'
    def command(args, **kwargs):
        calls.append((args, kwargs))
        if over_budget and args == ['./ops/database_parity/run_restore_drill.sh']:
            clock[0] = 3601
        if args[:2] == ['docker', 'build']:
            assert json.loads((proof_dir / 'post027-restore-native.json').read_text())['status'] == 'fail'
        if args[-1] == 'ops.database_parity.run_post027_restore':
            assert kwargs['env']['POST027_CANDIDATE_IMAGE'] == image
            assert kwargs['env']['POST027_STAGING'] == str(proof_dir.resolve())
            assert kwargs['timeout'] == support.PROOF_TIMEOUT + support.CLEANUP_ALLOWANCE
        return SimpleNamespace(stdout='3.3.5 pg_restore (PostgreSQL) 16. postgres'
            ' postgresql-runtime: PASS postgres-legacy-upgrades: PASS fixtures=5 postgres-offline-restore: PASS', returncode=0)
    monkeypatch.setattr(parity, '_command', command)
    assert parity.run('proof-handoff', tmp_path, 'owned-test-image') == int(over_budget)
    proof_calls = [i for i, call in enumerate(calls) if isinstance(call, tuple)
                   and call[0][-1] == 'ops.database_parity.run_post027_restore']
    if over_budget:
        assert not proof_calls
        assert json.loads((proof_dir / 'post027-restore-native.json').read_text())['status'] == 'fail'
    else:
        assert len(proof_calls) == 1
        assert proof_calls[0] < calls.index('image-cleanup')


@pytest.mark.parametrize('before,after', [
    ('maintenance_password_file=', 'nonexistent_password_argument='),
    ('from core.schema import bootstrap_database', 'from core.schema import nonexistent_bootstrap'),
])
def test_offline_wiring_rejects_broken_baseline_api(monkeypatch, before, after):
    monkeypatch.setattr(runner, 'BASELINE_BACKUP_SOURCE', runner.BASELINE_BACKUP_SOURCE.replace(before, after))
    with pytest.raises((TypeError, pytest.fail.Exception)):
        test_baseline_embedded_script_imports_and_signatures()


def test_receipt_replacement_preserves_failure_until_complete(tmp_path, monkeypatch):
    runner.write_incomplete_receipt(tmp_path)
    def interrupted(*args):
        raise InterruptedError('interrupted before replacing receipt')
    monkeypatch.setattr(Path, 'replace', interrupted)
    with pytest.raises(InterruptedError):
        runner.write_receipt(tmp_path, {'passed': True, 'status': 'pass'})
    receipt = json.loads((tmp_path / 'post027-restore-native.json').read_text())
    assert receipt['status'] == 'fail'
