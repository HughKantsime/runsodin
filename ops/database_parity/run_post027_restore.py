"""Owned PostgreSQL proof for post-027 migrations and preserved029 historical restores.

Docker cannot see the scratch worktree, so the backend/ops sources are copied (tracked
bytes plus working-tree edits) under artifacts/application-certification/migrations/ and
mounted read-only. Owned containers/network are named certfixMig_* and always removed.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import os

IMAGE = 'postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'
RUNTIME_REF = os.environ.get('POST027_RUNTIME_REF','ghcr.io/hughkantsime/odin@sha256:c3eabf35f38484b00c5818a4364495284bf4190d171e49daf53452ead4187a57')
RUNTIME_PLATFORM = os.environ.get('POST027_RUNTIME_PLATFORM','linux/amd64')
RELEASED_IMAGE = 'ghcr.io/hughkantsime/odin:v1.9.24'
import os
# Owners of concurrent proofs pick their own container prefix and receipt directory.
STAGING = Path(os.environ.get('POST027_STAGING', '/Users/ollama/Documents/Claude/odin/artifacts/application-certification/migrations'))
PREFIX = os.environ.get('POST027_CONTAINER_PREFIX', 'certfixMig_')


def run(*args, timeout=120):
    value = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if value.returncode:
        raise RuntimeError(value.stdout + value.stderr)
    return value.stdout


def main():
    root = Path.cwd().resolve()
    tag = uuid.uuid4().hex[:10]
    name = PREFIX + tag
    database, runtime = name + '_db', name + '_runtime'
    source = STAGING / ('src-' + tag)
    head = run('git', 'rev-parse', 'HEAD').strip()
    receipt = {'passed': False, 'source_head': head, 'source_status': run('git', 'status', '--short', '--', 'backend', 'ops', 'tests'),
               'postgres_image': IMAGE, 'runtime_ref': RUNTIME_REF,
               'scope': 'current-only' if os.environ.get('POST027_CURRENT_ONLY')=='1' else 'full',
               'runtime_provenance': 'Locally cached ODIN '+RUNTIME_PLATFORM+' runtime (python + pg clients); current source mounted read-only'}
    output = STAGING / 'post027-restore-native.json'
    try:
        files = [line for line in run('git', 'ls-files', '--cached', '--others', '--exclude-standard', 'backend', 'ops', 'docker').splitlines()
                 if not line.startswith(('backend/vision_models_default/', 'backend/static/'))]
        for relative in files:
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if (root / relative).is_file():
                shutil.copy2(root / relative, target)
        receipt['source_sha256'] = hashlib.sha256(''.join(f'{f}\0{hashlib.sha256((source / f).read_bytes()).hexdigest()}\n' for f in files if (source / f).is_file()).encode()).hexdigest()
        receipt['runtime_image'] = run('docker', 'image', 'inspect', '--format', '{{.Id}}', RUNTIME_REF).strip()
        run('docker', 'network', 'create', '--internal', name)
        run('docker', 'run', '-d', '--name', database, '--network', name, '--network-alias', 'pg',
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
                raise RuntimeError(created.stdout + created.stderr)
            time.sleep(1)
        receipt['released_image'] = run('docker', 'image', 'inspect', '--format', '{{.Id}}', RELEASED_IMAGE).strip()
        released = subprocess.run(['docker', 'run', '--rm', '--name', name + '_released', '--network', name, '--entrypoint', '/bin/sh', RELEASED_IMAGE, '-c',
            'umask 077; head -c 24 /dev/urandom | base64 > /tmp/pw; cd /app/backend && DATABASE_URL=postgresql://postgres@pg/post027_released '
            'DATABASE_PASSWORD_FILE=/tmp/pw ODIN_DB_ROLE=bootstrap JWT_SECRET_KEY=$(head -c 32 /dev/urandom | base64) python3 -m scripts.bootstrap_database'],
            text=True, capture_output=True, timeout=1800)
        assert released.returncode == 0, released.stdout[-2000:] + released.stderr[-4000:]
        receipt['released_bootstrap'] = json.loads(released.stdout.strip().splitlines()[-1])
        run('docker', 'exec', database, 'psql', '-U', 'postgres', '-d', 'post027_released', '-c',
            "INSERT INTO system_config(key,value) VALUES('historical_marker','\"released1924\"')")
        result = subprocess.run(['docker', 'run', '--rm', '--name', runtime, '--platform', RUNTIME_PLATFORM, '--network', name,
            '--user', '0', '--entrypoint', '/bin/sh', '-v', str(source) + ':/source:ro', '-e', 'PYTHONPATH=/source/backend',
            '-e', 'PYTHONPYCACHEPREFIX=/tmp/odin-post027-bytecode', '-e', 'BASE_URL=http://127.0.0.1:9', '-e', 'ODIN_CONTAINER_EXEC=/usr/bin/false',
            '-e', 'DEBUG=false', '-e', 'COOKIE_SECURE=false', '-e', 'POST027_CURRENT_ONLY='+os.environ.get('POST027_CURRENT_ONLY','0'), '-e', 'POST027_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres', receipt['runtime_image'],
            '-c', 'exec runuser -u odin -- python /source/ops/database_parity/post027_restore_native.py'],
            text=True, capture_output=True, timeout=5400)
        log = STAGING / 'post027-restore-native-stdout.log'
        log.write_text(result.stdout + '\n--- stderr ---\n' + result.stderr[-20000:])
        receipt['native_returncode'] = result.returncode
        receipt['native_facts'] = [line for line in result.stdout.splitlines() if line.startswith('post027-native-')]
        assert result.returncode == 0 and 'post027-native-pass' in result.stdout, result.stdout[-3000:] + result.stderr[-5000:]
        # Enterprise-compose ownership: root shell like the entrypoint, secrets owned by odin (10001), 0400.
        coordinator = subprocess.run(['docker', 'run', '--rm', '--name', runtime + '_coord', '--platform', RUNTIME_PLATFORM, '--network', name,
            '--user', '0', '--entrypoint', '/bin/sh', '-v', str(source) + ':/source:ro', '-e', 'PYTHONPATH=/source/backend',
            '-e', 'PYTHONPYCACHEPREFIX=/tmp/odin-post027-bytecode', '-e', 'BASE_URL=http://127.0.0.1:9', '-e', 'ODIN_CONTAINER_EXEC=/usr/bin/false',
            '-e', 'DEBUG=false', '-e', 'COOKIE_SECURE=false', '-e', 'POST027_CURRENT_ONLY='+os.environ.get('POST027_CURRENT_ONLY','0'), '-e', 'POST027_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres', '-w', '/source/backend',
            receipt['runtime_image'], '-c', 'exec python /source/ops/database_parity/post027_coordinator_native.py'],
            text=True, capture_output=True, timeout=5400)
        (STAGING / 'post027-coordinator-native-stdout.log').write_text(coordinator.stdout + '\n--- stderr ---\n' + coordinator.stderr[-20000:])
        receipt['coordinator_facts'] = [line for line in coordinator.stdout.splitlines() if line.startswith('post027-coordinator-')]
        assert coordinator.returncode == 0 and 'post027-coordinator-pass' in coordinator.stdout, coordinator.stdout[-3000:] + coordinator.stderr[-5000:]
        receipt['passed'] = True
    finally:
        for container in (runtime, runtime + '_coord', name + '_released'):
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=30)
        receipt['owned_database_removed'] = subprocess.run(['docker', 'rm', '-f', database], capture_output=True, timeout=30).returncode == 0
        receipt['owned_network_removed'] = subprocess.run(['docker', 'network', 'rm', name], capture_output=True, timeout=30).returncode == 0
        shutil.rmtree(source, ignore_errors=True)
        receipt['source_copy_removed'] = not source.exists()
        STAGING.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
