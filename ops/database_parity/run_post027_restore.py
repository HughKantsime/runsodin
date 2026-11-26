"""Native PostgreSQL restore proof, required by Trusted Validation CV05.

Run from the repository root with python -m ops.database_parity.run_post027_restore.
Only labelled resources with this proof's prefix may be removed.
"""
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import uuid

# Also support the historical direct-script entrypoint from the repository root.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ops.database_parity.restore_baselines import ROOT, load_baselines, select_baseline
from ops.release_gate.policy import redact_text
from ops.database_parity.restore_proof_support import DEFAULT_PREFIX, LABEL, owned_run, run, validate_facts

IMAGE = 'postgres@sha256:1d5b02d3a9941fbd52437db4107f8c52d6297dac553ca1e5c299ebb3756b4857'  # PostgreSQL 16.16 (Alpine); read from the image by Dev on 2026-11-20.
RELEASED_IMAGE = 'ghcr.io/hughkantsime/odin@' + next(
    row['image_digest'] for row in load_baselines()['releases'] if row['version'] == '1.9.24')

# No candidate source is imported by this process: all imports come from the
# digest-pinned previous release. Its native backup service writes the archive.
BASELINE_BACKUP_SOURCE = r'''
import json, os, secrets
from pathlib import Path
root = Path('/tmp/restore-proof'); root.mkdir(mode=0o755)
for name in ('app', 'maintenance'):
    path = root / name; path.write_text(secrets.token_urlsafe(32)); path.chmod(0o600)
os.environ.update(DATABASE_URL='postgresql://postgres@pg/post027_baseline', DATABASE_PASSWORD_FILE=str(root/'app'), ODIN_DATA_DIR=str(root), JWT_SECRET_KEY=secrets.token_urlsafe(48))
from sqlalchemy import text
from core.database_config import create_database_engine
from core.schema import bootstrap_database
from core.schema.bootstrap import POST027_MIGRATIONS
from modules.system.postgres_backup_service import create_postgres_backup
engine = create_database_engine(os.environ['DATABASE_URL'], role='restore', password_file=str(root/'app'))
bootstrap_database(engine)
with engine.begin() as c:
    c.execute(text("INSERT INTO system_config(key,value) VALUES('historical_marker',:value)"), {'value':json.dumps('previous-release')})
engine.dispose()
archive, metadata = create_postgres_backup(os.environ['DATABASE_URL'], maintenance_url='postgresql://postgres@pg/postgres', password_file=str(root/'app'), maintenance_password_file=str(root/'maintenance'), base_dir=root)
print(json.dumps({'archive':str(archive), 'schema_era':int(POST027_MIGRATIONS[-1][0].split(':')[1].split('-')[0])}))
'''


def prove(root, staging, source, name, prefix, baseline, receipt):
    database, runtime = name + '_db', name + '_runtime'
    label = LABEL + '=' + prefix
    runtime_ref = 'ghcr.io/hughkantsime/odin@' + baseline['image_digest']
    runtime_platform = os.environ.get('POST027_RUNTIME_PLATFORM', 'linux/amd64')
    candidate = os.environ.get('POST027_CANDIDATE_IMAGE', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', candidate):
        raise ValueError('The restore proof requires the built candidate image content ID')
    receipt['candidate_image'] = run('docker', 'image', 'inspect', '--format', '{{.Id}}', candidate).strip()
    if receipt['candidate_image'] != candidate:
        raise RuntimeError('The candidate image identity differs from the built image')
    files = [line for line in run('git', '-C', str(root), 'ls-files', '--cached', 'backend', 'ops', 'docker').splitlines()
             if not line.startswith(('backend/vision_models_default/', 'backend/static/'))
                 and not any(part.startswith('.env') for part in Path(line).parts)]
    for relative in files:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if (root / relative).is_file():
            shutil.copy2(root / relative, target)
    # Released sources come from Git objects, independent of each bundle under proof.
    for era in ('056', '057', '060', '062', '065'):
        receipt['released'+era+'_commit'] = json.loads((root / ('backend/core/schema/preserved'+era+'/manifest.json')).read_text())['source_commit']
        released = source / ('released'+era)
        released.mkdir(parents=True)
        tree = subprocess.run(['git', '-C', str(root), 'archive', '--format=tar', receipt['released'+era+'_commit'], 'backend/core', 'backend/modules', 'backend/license_manager.py', 'backend/main.py'], capture_output=True, check=True, timeout=120).stdout
        with tarfile.open(fileobj=io.BytesIO(tree)) as stream:
            stream.extractall(released, filter='data')
    receipt['source_sha256'] = hashlib.sha256(''.join(f'{f}\0{hashlib.sha256((source / f).read_bytes()).hexdigest()}\n' for f in files if (source / f).is_file()).encode()).hexdigest()
    pull_started = time.monotonic()
    for image in (IMAGE, runtime_ref, RELEASED_IMAGE):
        run('docker', 'pull', '--platform', runtime_platform, image, timeout=600)
    receipt['durations']['pull_seconds'] = round(time.monotonic() - pull_started, 3)
    receipt['runtime_image'] = run('docker', 'image', 'inspect', '--format', '{{.Id}}', runtime_ref).strip()
    run('docker', 'network', 'create', '--label', label, '--internal', name)
    run('docker', 'run', '-d', '--label', label, '--name', database, '--network', name, '--network-alias', 'pg',
        '--tmpfs', '/var/lib/postgresql/data:rw,size=1024m', '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', IMAGE)
    deadline = time.monotonic() + 60
    # TCP readiness: the image's init-time server listens on the socket only and then restarts.
    while subprocess.run(['docker', 'exec', database, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'], capture_output=True, timeout=10).returncode:
        if time.monotonic() > deadline:
            raise RuntimeError('Owned database not ready')
        time.sleep(.5)
    # The published v1.9.24 image's own bootstrap writes the released PostgreSQL database.
    # pg_isready can pass during the image's init phase, before its restart: retry briefly.
    deadline = time.monotonic() + 60
    while True:
        created = subprocess.run(['docker', 'exec', database, 'psql', '-U', 'postgres', '-c', 'CREATE DATABASE post027_released'],
                                 text=True, capture_output=True, timeout=30)
        if created.returncode == 0:
            break
        if time.monotonic() > deadline:
            raise RuntimeError('The historical release database could not be created before the timeout')
        time.sleep(1)
    receipt['released_image'] = run('docker', 'image', 'inspect', '--format', '{{.Id}}', RELEASED_IMAGE).strip()
    released = subprocess.run(['docker', 'run', '--rm', '--tmpfs', '/data:rw,mode=1777', '--label', label, '--name', name + '_released', '--network', name, '--entrypoint', '/bin/sh', RELEASED_IMAGE, '-c',
        'umask 077; head -c 24 /dev/urandom | base64 > /tmp/pw; cd /app/backend && DATABASE_URL=postgresql://postgres@pg/post027_released '
        'DATABASE_PASSWORD_FILE=/tmp/pw ODIN_DB_ROLE=bootstrap JWT_SECRET_KEY=$(head -c 32 /dev/urandom | base64) python3 -m scripts.bootstrap_database'],
        text=True, capture_output=True, timeout=1800)
    if released.returncode:
        raise RuntimeError('The historical release database could not be created')
    receipt['released_bootstrap'] = json.loads(released.stdout.strip().splitlines()[-1])
    run('docker', 'exec', database, 'psql', '-U', 'postgres', '-d', 'post027_released', '-c',
        "INSERT INTO system_config(key,value) VALUES('historical_marker','\"released1924\"')")
    # The previous release's installed code creates and validates its own backup.
    phase_started = time.monotonic()
    run('docker', 'exec', database, 'psql', '-U', 'postgres', '-c', 'CREATE DATABASE post027_baseline')
    produced = run('docker', 'run', '--tmpfs', '/data:rw,mode=1777', '--label', label, '--name', name + '_baseline',
                  '--platform', runtime_platform, '--network', name, '--user', '0',
                  '--entrypoint', 'python3', '-w', '/app/backend',
                  '-e', 'PYTHONPATH=/app/backend', '-e', 'DEBUG=false',
                  runtime_ref, '-c', BASELINE_BACKUP_SOURCE, timeout=1800)
    backup = json.loads(produced.strip().splitlines()[-1])
    if backup['schema_era'] != baseline['schema_era']:
        raise RuntimeError('The previous release image does not match its recorded schema era')
    run('docker', 'cp', name + '_baseline:' + backup['archive'], str(source / 'baseline.dump'))
    (source / 'baseline.dump').chmod(0o644)
    receipt['baseline_archive_sha256'] = hashlib.sha256((source / 'baseline.dump').read_bytes()).hexdigest()
    receipt['durations']['baseline_backup_seconds'] = round(time.monotonic() - phase_started, 3)
    phase_started = time.monotonic()
    result = subprocess.run(['docker', 'run', '--rm', '--tmpfs', '/data:rw,mode=1777', '--label', label, '--name', runtime, '--platform', runtime_platform, '--network', name,
        '--user', '0', '--entrypoint', '/bin/sh', '-v', str(source) + ':/source:ro', '-e', 'PYTHONPATH=/source/backend:/source',  # /source: the proof imports ops.database_parity.audit_integrity_native
        '-e', 'PYTHONPYCACHEPREFIX=/tmp/odin-post027-bytecode', '-e', 'BASE_URL=http://127.0.0.1:9', '-e', 'ODIN_CONTAINER_EXEC=/usr/bin/false',
        '-e', 'DEBUG=false', '-e', 'COOKIE_SECURE=false', '-e', 'POST027_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres',
        '-e', 'POST027_BASELINE_ERA='+str(baseline['schema_era']),
        '-e', 'POST027_RELEASED056_SOURCE=/source/released056/backend',
        '-e', 'POST027_RELEASED057_SOURCE=/source/released057/backend',
        '-e', 'POST027_RELEASED060_SOURCE=/source/released060/backend',
        '-e', 'POST027_RELEASED062_SOURCE=/source/released062/backend',
        '-e', 'POST027_RELEASED065_SOURCE=/source/released065/backend', receipt['candidate_image'],
        '-c', 'exec runuser -u odin -- python /source/ops/database_parity/post027_restore_native.py'],
        text=True, capture_output=True, timeout=5400)
    log = staging / 'post027-restore-native-stdout.log'
    log.write_text(redact_text(result.stdout + '\n--- stderr ---\n' + result.stderr[-20000:], ()))
    receipt['native_returncode'] = result.returncode
    if result.returncode:
        raise RuntimeError('The native restore proof failed; see its local log')
    receipt['native_facts'] = validate_facts(result.stdout, 'native')
    receipt['durations']['native_seconds'] = round(time.monotonic() - phase_started, 3)
    # Enterprise-compose ownership: root shell like the entrypoint, secrets owned by odin (10001), 0400.
    phase_started = time.monotonic()
    coordinator = subprocess.run(['docker', 'run', '--rm', '--tmpfs', '/data:rw,mode=1777', '--label', label, '--name', runtime + '_coord', '--platform', runtime_platform, '--network', name,
        '--user', '0', '--entrypoint', '/bin/sh', '-v', str(source) + ':/source:ro', '-e', 'PYTHONPATH=/source/backend',
        '-e', 'PYTHONPYCACHEPREFIX=/tmp/odin-post027-bytecode', '-e', 'BASE_URL=http://127.0.0.1:9', '-e', 'ODIN_CONTAINER_EXEC=/usr/bin/false',
        '-e', 'DEBUG=false', '-e', 'COOKIE_SECURE=false', '-e', 'POST027_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres', '-w', '/source/backend',
        receipt['candidate_image'], '-c', 'exec python /source/ops/database_parity/post027_coordinator_native.py'],
        text=True, capture_output=True, timeout=5400)
    (staging / 'post027-coordinator-native-stdout.log').write_text(redact_text(coordinator.stdout + '\n--- stderr ---\n' + coordinator.stderr[-20000:], ()))
    if coordinator.returncode:
        raise RuntimeError('The restore coordinator proof failed; see its local log')
    receipt['coordinator_facts'] = validate_facts(coordinator.stdout, 'coordinator')
    receipt['durations']['coordinator_seconds'] = round(time.monotonic() - phase_started, 3)


GENERATED_FIXTURES = (
    'ams-file-source-wire.json',
    'cert-A-printers-wire.json',
    'cert-B-wire.json',
    'cert-C-consumables-search-wire.json',
    'cert-C-consumables-wire.json',
    'cert-C-model-revisions-wire.json',
    'cert-C-models-wire.json',
    'cert-C-products-wire.json',
    'cert-C-spools-wire.json',
    'cert-C-upload-wire.json',
    'cert-C-vision-detections-wire.json',
    'cert-E-ana-analytics.json',
    'cert-E-ana-export-jobs.json',
    'cert-E-ana-failures.json',
    'cert-E-ana-models.json',
    'cert-E-ana-pricing-config.json',
    'cert-E-ana-print-jobs-energy.json',
    'cert-E-ana-stats-7.json',
    'cert-E-integ-branding.json',
    'cert-E-integ-ws-telemetry.json',
    'cert-E-notif-admin.json',
    'cert-E-notif-alerts.json',
    'cert-E-notif-org-settings.json',
    'cert-E-rep-chargeback-wire.json',
    'cert-E-rep-license-wire.json',
    'cert-E-rep-schedules-wire.json',
    'cert-E-rep-usage-wire.json',
    'certD-jobsA-approval-wire.json',
    'certD-jobsB-wire.json',
    'certD-jobsC-wire.json',
    'certDops-audit-wire.json',
    'certDops-backup-wire.json',
    'certDops-bulk-jobs-wire.json',
    'certDops-maintenance-status-wire.json',
    'chargeback-wire-owned.json',
    'public-license-wire-owned.json',
    'remote-access.json',
    's1-gcode-preview-wire.json',
    's1-start-next-suggestion-wire.json',
    'schedule-ams-wire.json',
    'trial-license-wire.json',
)

def source_dirty_paths(root):
    # CV02 writes these frontend JSON fixtures; none is part of the source copy.
    status = run('git', '-C', str(root), 'status', '--porcelain', '-z', '--untracked-files=all',
                 '--', '.', *(':(exclude)frontend/src/test-fixtures/' + name for name in GENERATED_FIXTURES))
    paths = []
    entries = iter(status.split('\0'))
    for entry in entries:
        if not entry:
            continue
        paths.append(entry[3:])  # XY status and a space precede the unquoted path.
        if 'R' in entry[:2] or 'C' in entry[:2]:
            paths.append(next(entries))  # -z emits the original path separately.
        if len(paths) >= 5:
            break
    return paths[:5]


def source_is_dirty(root):
    return bool(source_dirty_paths(root))


def write_receipt(staging, receipt):
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / 'post027-restore-native.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(receipt, indent=2) + '\n')
    temporary.replace(path)


def write_incomplete_receipt(staging):
    write_receipt(staging, {
        'passed': False, 'status': 'fail',
        'error': 'CV05 restore proof did not finish (not reached, interrupted or killed)',
    })


def main():
    started = time.monotonic()
    default_staging = Path(os.environ['ODIN_DATABASE_PARITY_ARTIFACT_ROOT']) / 'restore-proof' if os.environ.get('ODIN_DATABASE_PARITY_ARTIFACT_ROOT') else ROOT / 'artifacts/application-certification/migrations'
    staging = Path(os.environ.get('POST027_STAGING', default_staging)).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    prefix = os.environ.get('POST027_CONTAINER_PREFIX', DEFAULT_PREFIX)
    name = prefix + uuid.uuid4().hex[:12]
    source = None
    receipt = {'passed': False, 'status': 'fail', 'durations': {}, 'native_facts': [], 'coordinator_facts': []}
    # A SIGKILL cannot execute finally. Leave failure evidence before doing work.
    write_incomplete_receipt(staging)
    try:
        receipt['candidate_commit'] = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD').strip()
        receipt['source_dirty_paths'] = source_dirty_paths(ROOT)
        receipt['source_dirty'] = bool(receipt['source_dirty_paths'])
        if receipt['source_dirty']:
            raise ValueError('The restore proof refuses a dirty candidate source tree: '
                             + ', '.join(json.dumps(path) for path in receipt['source_dirty_paths']))
        receipt['candidate_version'] = (ROOT / 'VERSION').read_text().strip()
        baseline = select_baseline(receipt['candidate_version'], ROOT)
        receipt['baseline'] = baseline
        receipt['postgres_image'] = IMAGE
        if os.environ.get('POST027_CURRENT_ONLY') not in (None, '', '0') or os.environ.get('POST027_RUNTIME_REF'):
            raise ValueError('The required restore proof cannot skip checks or override its released image')
        with owned_run(prefix) as source:
            prove(ROOT, staging, source, name, prefix, baseline, receipt)
        receipt.update(passed=True, status='pass')
    except subprocess.TimeoutExpired:
        receipt['error'] = 'The restore proof timed out'
    except (Exception, KeyboardInterrupt) as error:
        receipt['error'] = str(error) if isinstance(error, (RuntimeError, ValueError, InterruptedError)) else 'The restore proof failed: ' + type(error).__name__
    finally:
        receipt['source_copy_removed'] = source is None or not source.exists()
        if not receipt['source_copy_removed']:
            receipt.update(passed=False, status='fail', error='The restore proof source copy could not be removed')
        receipt['durations']['total_seconds'] = round(time.monotonic() - started, 3)
        write_receipt(staging, receipt)
    print('PostgreSQL restore proof: ' + receipt['status'] + (': ' + receipt['error'] if 'error' in receipt else ''))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
