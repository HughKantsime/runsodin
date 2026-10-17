"""Owned PostgreSQL run of integer_flags_native.py; loopback-only database, removed afterwards.

Usage: python -m ops.database_parity.run_integer_flags <output-dir>
"""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from ops.database_parity.run_ams_adoption import IMAGE, run

root = Path.cwd().resolve()
suffix = secrets.token_hex(4)
database = f'intflags_pg_{suffix}'
paths = ['backend/modules/vision/inference_engine.py', 'backend/modules/vision/routes/vision_models.py',
         'backend/modules/vision/routes/detections.py', 'backend/modules/system/profile_routes.py',
         'backend/modules/system/profile_history.py', 'backend/core/db_compat.py',
         'ops/database_parity/integer_flags_native.py', 'ops/database_parity/run_integer_flags.py']
out_dir = Path(sys.argv[1]).resolve()
out_dir.mkdir(parents=True, exist_ok=True)
receipt = {'passed': False,
           'scope': 'Owned current PostgreSQL bootstrap; integer flag columns through the real code: vision model registry load, '
                    'activation and retirement, per-printer vision settings and printer profiles with JSON booleans',
           'source_sha256': {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}, 'postgres_image': IMAGE,
           'runtime_provenance': 'Worktree interpreter (.venv-stress, psycopg) against the pinned PostgreSQL image on 127.0.0.1',
           'external_stubs': 'Principal injected at get_current_user; no hardware, no network'}
password = secrets.token_urlsafe(32)
env = {**os.environ, 'POSTGRES_PASSWORD': password}
log = out_dir / 'integer-flags-native.log'
try:
    subprocess.run(['docker', 'run', '-d', '--name', database, '-p', '127.0.0.1::5432',
                    '--tmpfs', '/var/lib/postgresql/data:rw,size=128m', '-e', 'POSTGRES_PASSWORD', IMAGE],
                   env=env, check=True, capture_output=True, timeout=120)
    deadline = time.monotonic() + 180

    def ready():
        try:
            return subprocess.run(['docker', 'exec', database, 'pg_isready', '-U', 'postgres', '-h', '127.0.0.1'],
                                  capture_output=True, timeout=20).returncode == 0
        except subprocess.TimeoutExpired:
            return False
    while not ready():
        if time.monotonic() > deadline:
            raise RuntimeError('Owned PostgreSQL not ready')
        time.sleep(1)
    time.sleep(1)
    port = run('docker', 'port', database, '5432/tcp').strip().splitlines()[0].rsplit(':', 1)[1]
    receipt['postgres_server_version'] = run('docker', 'exec', database, 'postgres', '--version').strip()
    result = subprocess.run([sys.executable, str(root / 'ops/database_parity/integer_flags_native.py')], cwd=root,
                            env={**env, 'INTF_PG_HOST': '127.0.0.1', 'INTF_PG_PORT': port, 'INTF_PG_PASSWORD': password,
                                 'INTF_BACKEND': str(root / 'backend'),
                                 'PYTHONPATH': f'{root / "backend"}:{root}', 'PYTHONDONTWRITEBYTECODE': '1'},
                            capture_output=True, timeout=900)
    text_out = result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace')
    log.write_text(text_out.replace(password, '<redacted>'))
    receipt['test_summary'] = [line for line in text_out.splitlines()
                               if line.endswith(' ok') or line.startswith(('integer-flags', 'bootstrap ok', 'AssertionError'))]
    if result.returncode or 'integer-flags-native-pass' not in text_out:
        raise RuntimeError('Owned native integer-flag probe failed; see log')
    assert all(hashlib.sha256((root / p).read_bytes()).hexdigest() == h for p, h in receipt['source_sha256'].items())
    receipt['passed'] = True
except BaseException as error:
    receipt['error_type'] = type(error).__name__
    raise
finally:
    receipt['owned_database_removed'] = subprocess.run(['docker', 'rm', '-f', database], capture_output=True,
                                                       timeout=60).returncode == 0
    (out_dir / 'integer-flags-native.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'passed': receipt['passed'], 'cleanup': receipt['owned_database_removed']}))
