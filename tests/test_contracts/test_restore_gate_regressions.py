"""Offline regressions from the first real restore-gate run."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('jwt_present', [False, True])
def test_schema_bootstrap_does_not_import_auth(tmp_path, jwt_present):
    environment = {
        'PATH': os.defpath,
        'PYTHONPATH': str(ROOT / 'backend'),
        'DATABASE_URL': 'sqlite:///' + str(tmp_path / 'offline.db'),
        'ODIN_AUDIT_KEY_DIR': str(tmp_path / 'audit'),
    }
    if jwt_present:
        environment['JWT_SECRET_KEY'] = 'synthetic-bootstrap-import-test'
    result = subprocess.run(
        [sys.executable, '-c', '''
import os, sys
from sqlalchemy import create_engine
from core.schema.bootstrap import bootstrap_database
engine = create_engine(os.environ['DATABASE_URL'])
try:
    result = bootstrap_database(engine)
    assert result['dialect'] == 'sqlite' and result['applied']
    assert 'core.auth' not in sys.modules, 'bootstrap imported core.auth'
    assert 'core.rbac' not in sys.modules, 'bootstrap imported core.rbac'
finally:
    engine.dispose()
'''], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_bootstrap_inventory_imports_use_the_service_boundary():
    from tests.test_contracts.test_no_cross_module_imports import _is_allowed

    source = (ROOT / 'backend/core/schema/bootstrap.py').read_text()
    imports = [node for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.ImportFrom)
               and any(alias.name == 'backfill_catalogue_materials' for alias in node.names)]
    assert len(imports) == 1
    assert _is_allowed(ast.get_source_segment(source, imports[0]))


def test_inventory_keeps_the_public_backfill_reexport():
    from modules.inventory.bootstrap.services import backfill_catalogue_materials
    from modules.inventory.services import backfill_catalogue_materials as public

    assert public is backfill_catalogue_materials


@pytest.mark.parametrize('attempt', [1, 2])
@pytest.mark.parametrize('failure', ['exit', 'timeout'])
def test_failed_parity_drill_retains_full_redacted_output(tmp_path, monkeypatch, capsys, attempt, failure):
    from ops.database_parity import runner as parity

    image = 'sha256:' + 'c' * 64
    secret = 'synthetic-parity-log-secret'
    output = 'original failure before the tail\n' + ('diagnostic detail\n' * 200) + secret + '\nlast line\n'
    secret_file = tmp_path / 'owned-input'
    secret_file.touch()
    monkeypatch.setattr(parity, '_generate_parity_secrets', lambda: {'API_KEY': secret})
    monkeypatch.setattr(parity, '_write_secret_bundle', lambda _: secret_file)
    cleaned = []
    lifecycle = SimpleNamespace(
        begin=lambda: None, mark_build_attempted=lambda: None,
        docker_build_owner_args=lambda: [], establish_final_image=lambda _: None,
        freeze_ownership=lambda: None, finalize=lambda: cleaned.append(True) or [],
        evidence=lambda: {}, ownership_capture_attempted=True, final_image_id=image,
    )
    monkeypatch.setattr(parity, 'DisposableImageLifecycle', lambda *a, **k: lifecycle)
    monkeypatch.setattr(parity, '_docker_resources', lambda _: set())
    monkeypatch.setattr(parity, 'read_iidfile', lambda _: image)
    monkeypatch.setattr(parity, 'verify_built_image', lambda *a, **k: image)
    monkeypatch.setattr(parity, '_assert_image_metadata', lambda _: image)
    monkeypatch.setattr(parity, '_run_sqlite_exact_image', lambda *a: ('ok', {}))
    monkeypatch.setattr(parity, 'inspect_junit', lambda _: {'passed': 1})
    monkeypatch.setattr(parity, '_extract_database_evidence', lambda _: {})
    monkeypatch.setattr(parity, '_extract_topology_evidence', lambda *a: {})
    calls = []
    drill_count = 0

    def command(args, **kwargs):
        nonlocal drill_count
        calls.append(args)
        if args == ['./ops/database_parity/run_restore_drill.sh']:
            drill_count += 1
            if drill_count == attempt:
                if failure == 'timeout':
                    # TimeoutExpired carries bytes even when text=True.
                    raise subprocess.TimeoutExpired(args, kwargs['timeout'], output=output.encode())
                return subprocess.CompletedProcess(args, 23, output)
        return subprocess.CompletedProcess(args, 0,
            '3.3.5 pg_restore (PostgreSQL) 16. postgresql-runtime: PASS '
            'postgres-legacy-upgrades: PASS fixtures=5 postgres-offline-restore: PASS')

    monkeypatch.setattr(parity.subprocess, 'run', command)
    assert parity.run('failed-drill', tmp_path, 'owned-test-image') == 1
    diagnostics = capsys.readouterr()
    artifact_dir = tmp_path / 'failed-drill'
    log = artifact_dir / f'postgres-{attempt}.log'
    assert log.exists(), 'failed drill output was lost'
    assert log.read_text() == parity.redact_text(output, [secret])
    assert secret not in log.read_text()
    manifest = json.loads((artifact_dir / 'manifest.json').read_text())
    assert manifest['status'] == 'FAIL'
    phase = next(p for p in manifest['phases'] if p['name'] == f'postgres-exact-image-{attempt}')
    assert phase['status'] == 'FAIL'
    assert ('command failed (23)' if failure == 'exit' else 'command timed out') in diagnostics.err
    assert secret not in json.dumps(manifest)
    assert drill_count == attempt
    assert not any(args[-1] == 'ops.database_parity.run_post027_restore' for args in calls)
    assert cleaned == [True]
    assert not secret_file.exists()


def test_parity_runtime_files_leave_candidate_clean_at_proof(tmp_path, monkeypatch):
    from ops.database_parity import runner as parity
    from ops.database_parity import run_post027_restore as proof

    root = tmp_path / 'repo'
    root.mkdir()
    (root / '.gitignore').write_text((ROOT / '.gitignore').read_text())
    tracked = root / 'candidate.py'
    tracked.write_text('original\n')
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, text=True)
    git('init', '-q')
    git('add', '.gitignore', tracked.name)
    git('-c', 'user.name=Offline test', '-c', 'user.email=offline@example.invalid',
        '-c', 'core.hooksPath=/dev/null', 'commit', '--no-gpg-sign', '-qm', 'fixture')
    monkeypatch.setattr(parity, 'ROOT', root)
    # Use the real secret-bundle writer with no credential values.
    monkeypatch.setattr(parity, '_generate_parity_secrets', lambda: {})
    image = 'sha256:' + 'c' * 64
    lifecycle = SimpleNamespace(
        begin=lambda: None, mark_build_attempted=lambda: None,
        docker_build_owner_args=lambda: [], establish_final_image=lambda _: None,
        freeze_ownership=lambda: None, finalize=lambda: [], evidence=lambda: {},
        ownership_capture_attempted=True, final_image_id=image,
    )
    monkeypatch.setattr(parity, 'DisposableImageLifecycle', lambda *a, **k: lifecycle)
    monkeypatch.setattr(parity, '_docker_resources', lambda _: set())
    monkeypatch.setattr(parity, 'read_iidfile', lambda _: image)
    monkeypatch.setattr(parity, 'verify_built_image', lambda *a, **k: image)
    monkeypatch.setattr(parity, '_assert_image_metadata', lambda _: image)
    monkeypatch.setattr(parity, '_run_sqlite_exact_image', lambda *a: ('ok', {}))
    monkeypatch.setattr(parity, 'inspect_junit', lambda _: {'passed': 1})
    monkeypatch.setattr(parity, '_extract_database_evidence', lambda _: {})
    monkeypatch.setattr(parity, '_extract_topology_evidence', lambda *a: {})
    observations = []
    bundles = []
    def command(args, **kwargs):
        if args[0] == 'git':
            return git(*args[1:])
        if args == ['./ops/database_parity/run_restore_drill.sh']:
            bundle = Path(kwargs['env']['ODIN_PARITY_SECRET_INPUT_FILE'])
            assert bundle.is_file()
            bundles.append(bundle)
        if any(arg.startswith('--junitxml=') for arg in args):
            Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('--junitxml='))).write_text('<testsuites/>')
        if args[-1] == 'ops.database_parity.run_post027_restore':
            assert all(bundle.exists() for bundle in bundles)
            observations.append(proof.source_is_dirty(root))
            tracked.write_text('modified\n')
            observations.append(proof.source_is_dirty(root))
            tracked.write_text('original\n')
        return subprocess.CompletedProcess(args, 0,
            '3.3.5 pg_restore (PostgreSQL) 16. postgresql-runtime: PASS '
            'postgres-legacy-upgrades: PASS fixtures=5 postgres-offline-restore: PASS')

    monkeypatch.setattr(parity, '_command', command)
    # The runner itself creates the bundle, evidence directories, logs and receipt.
    assert parity.run('clean-handoff', root / 'artifacts/database-parity', 'owned-test-image') == 0
    assert observations == [False, True]
    assert len(bundles) == 2 and not bundles[0].exists()
