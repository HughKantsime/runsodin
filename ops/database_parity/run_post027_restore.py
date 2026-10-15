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

IMAGE = 'postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'
RUNTIME_REF = 'ghcr.io/hughkantsime/odin@sha256:f4ecb5d6dd0b8b8216fb29c680a887624714f79c0e7236e6ec20b22e2ca2899e'
STAGING = Path('/Users/ollama/Documents/Claude/odin/artifacts/application-certification/migrations')


def run(*args, timeout=120):
    value = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if value.returncode:
        raise RuntimeError(value.stdout + value.stderr)
    return value.stdout


def main():
    root = Path.cwd().resolve()
    tag = uuid.uuid4().hex[:10]
    name = 'certfixMig_' + tag
    database, runtime = name + '_db', name + '_runtime'
    source = STAGING / ('src-' + tag)
    head = run('git', 'rev-parse', 'HEAD').strip()
    receipt = {'passed': False, 'source_head': head, 'source_status': run('git', 'status', '--short', '--', 'backend', 'ops', 'tests'),
               'postgres_image': IMAGE, 'runtime_ref': RUNTIME_REF,
               'runtime_provenance': 'Published v1.9.24 AMD64 runtime (python + pg clients); current source mounted read-only'}
    output = STAGING / 'post027-restore-native.json'
    try:
        files = [line for line in run('git', 'ls-files', '--cached', '--others', '--exclude-standard', 'backend', 'ops').splitlines()
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
        while subprocess.run(['docker', 'exec', database, 'pg_isready', '-U', 'postgres'], capture_output=True, timeout=10).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError('Owned database not ready')
            time.sleep(.5)
        result = subprocess.run(['docker', 'run', '--rm', '--name', runtime, '--platform', 'linux/amd64', '--network', name,
            '--user', '0', '--entrypoint', '/bin/sh', '-v', str(source) + ':/source:ro', '-e', 'PYTHONPATH=/source/backend',
            '-e', 'PYTHONPYCACHEPREFIX=/tmp/odin-post027-bytecode', '-e', 'BASE_URL=http://127.0.0.1:9', '-e', 'ODIN_CONTAINER_EXEC=/usr/bin/false',
            '-e', 'DEBUG=false', '-e', 'COOKIE_SECURE=false', '-e', 'POST027_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres', receipt['runtime_image'],
            '-c', 'exec runuser -u odin -- python /source/ops/database_parity/post027_restore_native.py'],
            text=True, capture_output=True, timeout=5400)
        log = STAGING / 'post027-restore-native-stdout.log'
        log.write_text(result.stdout + '\n--- stderr ---\n' + result.stderr[-20000:])
        receipt['native_returncode'] = result.returncode
        receipt['native_facts'] = [line for line in result.stdout.splitlines() if line.startswith('post027-native-')]
        assert result.returncode == 0 and 'post027-native-pass' in result.stdout, result.stdout[-3000:] + result.stderr[-5000:]
        receipt['passed'] = True
    finally:
        subprocess.run(['docker', 'rm', '-f', runtime], capture_output=True, timeout=30)
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
