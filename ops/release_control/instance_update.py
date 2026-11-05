"""Run via v4's SSH stdin, only inside its owned deploy window for mutations.

No daemon restart, builds, registry writes, or changes to other services. The
standard Compose override disables Watchtower for odin; unknown overrides fail
closed. stdout contains only image IDs and rollback commands, never config/env.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

DIRECTORY = Path('/opt/odin/runsodin')
LABEL = 'com.centurylinklabs.watchtower.enable'
OVERRIDE = {'services': {'odin': {'labels': {LABEL: 'false'}}}}
IMAGE = 'ghcr.io/hughkantsime/odin'


def command(*args):
    result = subprocess.run(list(args), text=True, capture_output=True, timeout=(600 if 'pull' in args else 300 if 'up' in args else 30))
    if result.returncode:
        raise RuntimeError(f'{args[0]} {args[1]} failed (output withheld)')
    return result.stdout.strip()


def compose(*args, override=None):
    files = ['-f', 'docker-compose.yml']
    if Path('docker-compose.override.yml').exists():
        files += ['-f', 'docker-compose.override.yml']
    if override:
        files += ['-f', override]
    return command('docker', 'compose', *files, *args)


def inspect():
    return json.loads(command('docker', 'inspect', 'odin'))[0]


def check():
    config = json.loads(compose('config', '--format', 'json'))
    if str(config['services']['odin'].get('labels', {}).get(LABEL)).lower() != 'false':
        raise RuntimeError('Compose must disable Watchtower for odin; use --migrate-watchtower')
    if inspect()['Config'].get('Labels', {}).get(LABEL) != 'false':
        raise RuntimeError('running odin still follows Watchtower; use --migrate-watchtower')


def override_file(image):
    descriptor, name = tempfile.mkstemp(prefix='.odin-release-', suffix='.json', dir='.')
    with os.fdopen(descriptor, 'w') as stream:
        json.dump({'services': {'odin': {'image': image, 'labels': {LABEL: 'false'}}}}, stream)
    return name


def rollback_file(old_id, token):
    # Retained after an unsuccessful update so the printed command remains usable.
    name = f'.odin-rollback-{token}.json'
    with open(name, 'x') as stream:
        json.dump({'services': {'odin': {'image': old_id, 'labels': {LABEL: 'false'}}}}, stream)
    os.chmod(name, 0o600)
    override = ' -f docker-compose.override.yml' if Path('docker-compose.override.yml').exists() else ''
    print(f'Rollback (inside a safe deploy window): cd {DIRECTORY} && docker compose -f docker-compose.yml{override} -f {name} up -d --no-deps --pull never odin', flush=True)


def perform(action, digest, token):
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        raise ValueError('invalid operation token')
    os.chdir(DIRECTORY)
    if action == 'check':
        check()
        return
    if action == 'migrate':
        # Never overwrite an operator's override; the exact v4-owned content is idempotent.
        path = Path('docker-compose.override.yml')
        if path.exists():
            if json.loads(path.read_text()) != OVERRIDE:
                raise RuntimeError('existing Compose override requires operator reconciliation')
        else:
            # Other conventional override files could change what plain compose loads.
            if Path('compose.override.yaml').exists() or Path('compose.override.yml').exists() or Path('docker-compose.override.yaml').exists():
                raise RuntimeError('existing Compose override requires operator reconciliation')
            with path.open('x') as stream:
                json.dump(OVERRIDE, stream)
            path.chmod(0o600)
        old_id = inspect()['Image']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', old_id):
            raise RuntimeError('invalid running image ID')
        rollback_file(old_id, token)
        temporary = override_file(old_id)
        try:
            # Recreate the SAME local image; migration must not pull a newer latest.
            compose('up', '-d', '--no-deps', '--pull', 'never', 'odin', override=temporary)
            if inspect()['Image'] != old_id:
                raise RuntimeError('migration changed the running image')
            check()
        finally:
            os.unlink(temporary)
        return
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('invalid target digest')
    check()
    reference = f'{IMAGE}@{digest}'
    temporary = override_file(reference)
    try:
        if action == 'pull':
            compose('pull', 'odin', override=temporary)
        elif action == 'apply':
            old_id = inspect()['Image']
            rollback_file(old_id, token)
            desired = json.loads(command('docker', 'image', 'inspect', reference))[0]
            if reference not in desired.get('RepoDigests', []):
                raise RuntimeError('pulled image digest mismatch')
            compose('up', '-d', '--no-deps', '--pull', 'never', 'odin', override=temporary)
            running = inspect()
            if running['Image'] != desired['Id'] or running['Config']['Image'] != reference:
                raise RuntimeError('running image does not match released digest')
            check()
        else:
            raise ValueError('unknown action')
    finally:
        os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'migrate', 'pull', 'apply'))
    parser.add_argument('--digest', default='')
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    perform(args.action, args.digest, args.token)
