"""Exercise bootstrap install decisions without installing dependencies or browsers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def bootstrap(tmp_path):
    script = tmp_path / 'ops/release_control/bootstrap_validation_deps.sh'
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / 'ops/release_control/bootstrap_validation_deps.sh', script)
    for name in ('backend/requirements.txt', 'ops/hardware_certification/requirements.txt',
                 'tests/requirements-contracts.txt', 'frontend/package.json', 'frontend/package-lock.json'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    tools = tmp_path / 'tools'
    tools.mkdir()
    source = f'#!{sys.executable}\n' + '''import hashlib, json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'sha256sum':
    if args:
        for name in args:
            print(hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest(), name)
    else:
        print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest(), '-')
    sys.exit(0)
with open(os.environ['BOOTSTRAP_LOG'], 'a') as log:
    log.write(json.dumps([sys.argv[0], *args]) + '\\n')
if name == 'python3.11' and args[:2] == ['-m', 'venv']:
    venv = pathlib.Path(args[2]) / 'bin'
    venv.mkdir(parents=True, exist_ok=True)
    for executable in ('python', 'pip'):
        target = venv / executable
        target.write_text(pathlib.Path(__file__).read_text())
        target.chmod(0o755)
if name == 'npm':
    pathlib.Path('frontend/node_modules').mkdir(parents=True, exist_ok=True)
if name == 'pip' and os.environ.get('FAIL_PIP'):
    sys.exit(17)
'''
    for name in ('python3.11', 'npm', 'npx', 'sha256sum'):
        path = tools / name
        path.write_text(source)
        path.chmod(0o755)
    log = tmp_path / 'calls.jsonl'

    def run(fail=False):
        log.write_text('')
        result = subprocess.run(['bash', str(script)], cwd=tmp_path, capture_output=True, text=True,
                                env={**os.environ, 'PATH': f'{tools}:/usr/bin:/bin',
                                     'BOOTSTRAP_LOG': str(log), 'FAIL_PIP': '1' if fail else ''})
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls
    return tmp_path, run


def installs(calls):
    return [Path(call[0]).parent.parent.name for call in calls if Path(call[0]).name == 'pip']


def test_bootstrap_clean_checkout_noop_and_changed_inputs(bootstrap):
    root, run = bootstrap
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert installs(calls) == ['.hardware-cert-venv', '.venv-stress', '.venv-contracts']
    assert any(Path(call[0]).name == 'npm' for call in calls)
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert installs(calls) == []
    assert not any(Path(call[0]).name == 'npm' for call in calls)
    # Browser caches can disappear independently of stamps; both are checked.
    assert sum(call[-3:] == ['playwright', 'install', 'chromium'] for call in calls) == 2
    for changed, expected in (
        ('backend/requirements.txt', ['.hardware-cert-venv', '.venv-stress', '.venv-contracts']),
        ('ops/hardware_certification/requirements.txt', ['.hardware-cert-venv']),
        ('tests/requirements-contracts.txt', ['.venv-contracts']),
    ):
        (root / changed).write_text('changed')
        result, calls = run()
        assert result.returncode == 0, result.stderr
        assert installs(calls) == expected
    (root / 'frontend/package-lock.json').write_text('changed')
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert installs(calls) == []
    assert any(Path(call[0]).name == 'npm' for call in calls)
    (root / '.venv-stress/bin/python').unlink()
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert installs(calls) == ['.venv-stress']


def test_failed_install_leaves_no_success_stamp_and_retries(bootstrap):
    root, run = bootstrap
    result, _ = run()
    assert result.returncode == 0, result.stderr
    (root / 'backend/requirements.txt').write_text('changed')
    result, _ = run(fail=True)
    assert result.returncode == 17
    assert not (root / '.hardware-cert-venv/.validation-deps.sha256').exists()
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert installs(calls) == ['.hardware-cert-venv', '.venv-stress', '.venv-contracts']
