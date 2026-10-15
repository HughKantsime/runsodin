"""Owned PostgreSQL probe runner for cert-C fixes; loopback-only database, removed afterwards."""
import hashlib, json, os, secrets, subprocess, sys, time
from pathlib import Path
from ops.database_parity.run_ams_adoption import IMAGE, run
root = Path.cwd().resolve()
suffix = secrets.token_hex(4)
network, database, runtime = f'certfixC_net_{suffix}', f'certfixC_pg_{suffix}', f'certfixC_runtime_{suffix}'
paths = ['backend/core/schema/migrations/030_spoolman_link_uniqueness.py', 'backend/modules/inventory/routes/spools.py',
         'backend/modules/inventory/routes/_helpers.py', 'backend/modules/inventory/routes/drying_logs.py',
         'backend/modules/models_library/services.py', 'backend/modules/orders/product_costing.py',
         'backend/modules/vision/routes/detections.py', 'backend/modules/vision/monitor.py', 'backend/modules/vision/frame_storage.py',
         'backend/core/errors.py', 'ops/database_parity/certc_fixes_native.py', 'ops/database_parity/run_certc_fixes.py']
out_dir = Path(sys.argv[1]).resolve()
receipt = {'passed': False, 'scope': 'Owned current PostgreSQL bootstrap; cert-C fixes over real inventory and vision routers: migration 030 partial unique index (apply, idempotence, shape, duplicate refusal, enforcement), 409 mapping of the unique violation, int32 bound, superadmin rate/vision gates, deterministic finite material rate, per-piece COGS, vision_settings JSON persistence, global switch and retention cleanup',
           'source_sha256': {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}, 'postgres_image': IMAGE,
           'runtime_provenance': 'Worktree interpreter (.venv-stress, psycopg) against the pinned PostgreSQL image on 127.0.0.1; shared Docker VM had no memory for an emulated runtime',
           'external_stubs': 'Principal injected at get_current_user; no hardware, no network'}
password = secrets.token_urlsafe(32)
env = {**os.environ, 'POSTGRES_PASSWORD': password, 'CERTC_PG_PASSWORD': password}
log = out_dir / 'certc-fixes-native.log'
try:
    # The shared Docker VM is memory-bound (other agents' stacks), so the probe runs in this
    # checkout's own interpreter against the owned PostgreSQL bound to loopback only.
    run('docker', 'network', 'create', network)
    subprocess.run(['docker', 'run', '-d', '--name', database, '--network', network, '-p', '127.0.0.1::5432',
                    '--tmpfs', '/var/lib/postgresql/data:rw,size=128m', '-e', 'POSTGRES_PASSWORD', IMAGE],
                   env=env, check=True, capture_output=True, timeout=60)
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
    result = subprocess.run([sys.executable, str(root / 'ops/database_parity/certc_fixes_native.py')], cwd=root,
                            env={**env, 'CERTC_PG_HOST': '127.0.0.1', 'CERTC_PG_PORT': port, 'CERTC_BACKEND': str(root / 'backend'),
                                 'PYTHONPATH': f'{root / "backend"}:{root}', 'PYTHONDONTWRITEBYTECODE': '1'},
                            capture_output=True, timeout=900)
    text_out = result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace')
    log.write_text(text_out.replace(password, '<redacted>'))
    if result.returncode or 'certc-fixes-native-pass' not in text_out:
        raise RuntimeError('Owned native cert-C probe failed; see log')
    receipt['test_summary'] = [line for line in text_out.splitlines() if line.endswith(' ok') or line.startswith('certc-') or line == 'bootstrap ok']
    assert all(hashlib.sha256((root / p).read_bytes()).hexdigest() == h for p, h in receipt['source_sha256'].items())
    receipt['passed'] = True
except BaseException as error:
    receipt['error_type'] = type(error).__name__
    raise
finally:
    subprocess.run(['docker', 'rm', '-f', runtime], capture_output=True, timeout=30)
    receipt['owned_database_removed'] = subprocess.run(['docker', 'rm', '-f', database], capture_output=True, timeout=30).returncode == 0
    receipt['owned_network_removed'] = subprocess.run(['docker', 'network', 'rm', network], capture_output=True, timeout=30).returncode == 0
    (out_dir / 'certc-fixes-native.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'passed': receipt['passed'], 'cleanup': receipt['owned_database_removed'] and receipt['owned_network_removed']}))
