"""Run via v4's SSH stdin, inside its owned deploy window for restarts; pulls precede the window.

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
import sys
import tempfile

DIRECTORY = Path('/opt/odin/runsodin')
LABEL = 'com.centurylinklabs.watchtower.enable'
OVERRIDE = {'services': {'odin': {'labels': {LABEL: 'false'}}}}
IMAGE = 'ghcr.io/hughkantsime/odin'
APPLY_REFUSED = 4  # No compose up was attempted; safe to release deploy protection.


class ApplyRefused(RuntimeError):
    pass


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
    running = inspect()
    if running['Config'].get('Labels', {}).get(LABEL) != 'false':
        raise RuntimeError('running odin still follows Watchtower; use --migrate-watchtower')
    if config['services']['odin']['image'] != running['Config']['Image']:
        raise RuntimeError('Compose image differs from running Config.Image')


def owned_override():
    path = Path('docker-compose.override.yml')
    if path.exists():
        value = json.loads(path.read_text())
        service = value.get('services', {}).get('odin', {})
        image = service.get('image')
        expected = {'services': {'odin': {'labels': {LABEL: 'false'}, 'image': image}}}
        if value != OVERRIDE and not (value == expected and isinstance(image, str)
                and re.fullmatch(re.escape(IMAGE) + r'@sha256:[0-9a-f]{64}', image)):
            raise RuntimeError('existing Compose override requires operator reconciliation')
    elif any(Path(name).exists() for name in
             ('compose.override.yaml', 'compose.override.yml', 'docker-compose.override.yaml')):
        raise RuntimeError('existing Compose override requires operator reconciliation')
    return path


def pin_override(reference):
    path = owned_override()
    temporary = override_file(reference)
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def repo_reference(image_id):
    image = json.loads(command('docker', 'image', 'inspect', image_id))[0]
    if image['Id'] != image_id:
        raise RuntimeError('old image identity mismatch')
    references = [ref for ref in image.get('RepoDigests', [])
                  if re.fullmatch(re.escape(IMAGE) + r'@sha256:[0-9a-f]{64}', ref)]
    if not references:
        raise RuntimeError('old image has no ODIN RepoDigest for rollback')
    return references[0]


def override_file(image):
    descriptor, name = tempfile.mkstemp(prefix='.odin-release-', suffix='.json', dir='.')
    with os.fdopen(descriptor, 'w') as stream:
        json.dump({'services': {'odin': {'image': image, 'labels': {LABEL: 'false'}}}}, stream)
    return name


def rollback_file(old_reference, token):
    # Retained after an unsuccessful update so the printed command remains usable.
    name = f'.odin-rollback-{token}.json'
    with open(name, 'x') as stream:
        json.dump({'services': {'odin': {'image': old_reference, 'labels': {LABEL: 'false'}}}}, stream)
    os.chmod(name, 0o600)
    print(f'Rollback file retained: {name}; use instance_update.py rollback --token {token} inside a deploy window (the ODIN freeze lapses after 30 minutes)', flush=True)


def validate_operation(token):
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        raise ValueError('invalid operation token')
    os.chdir(DIRECTORY)


def local_image(reference):
    desired = json.loads(command('docker', 'image', 'inspect', reference))[0]
    if reference not in desired.get('RepoDigests', []):
        raise RuntimeError('local image digest mismatch')
    return desired


def verify_and_pin(desired, reference):
    running = inspect()
    if running['Image'] != desired['Id'] or running['Config']['Image'] != reference:
        raise RuntimeError('running image does not match requested digest')
    if running['Config'].get('Labels', {}).get(LABEL) != 'false':
        raise RuntimeError('running odin still follows Watchtower')
    pin_override(reference)
    check()


def apply_instance(digest, token):
    temporary = None
    try:
        validate_operation(token)
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('invalid target digest')
        check()
        owned_override()
        reference = f'{IMAGE}@{digest}'
        desired = local_image(reference)
        old_reference = repo_reference(inspect()['Image'])
        temporary = override_file(reference)
        rollback_file(old_reference, token)
    except Exception as error:
        if temporary is not None:
            os.unlink(temporary)
        # No restart has been attempted, even if a preflight read timed out.
        raise ApplyRefused('apply refused before restart (details withheld)') from error
    try:
        compose('up', '-d', '--no-deps', '--pull', 'never', 'odin', override=temporary)
        verify_and_pin(desired, reference)
    finally:
        os.unlink(temporary)


def rollback(token):
    owned_override()
    name = f'.odin-rollback-{token}.json'
    saved = json.loads(Path(name).read_text())
    reference = saved['services']['odin']['image']
    if (not isinstance(reference, str)
            or not re.fullmatch(re.escape(IMAGE) + r'@sha256:[0-9a-f]{64}', reference)
            or saved != {'services': {'odin': {'image': reference, 'labels': {LABEL: 'false'}}}}):
        raise RuntimeError('invalid rollback override')
    desired = local_image(reference)
    compose('up', '-d', '--no-deps', '--pull', 'never', 'odin', override=name)
    verify_and_pin(desired, reference)


def perform(action, digest, token):
    if action == 'apply':
        return apply_instance(digest, token)
    validate_operation(token)
    if action == 'rollback':
        return rollback(token)
    if action == 'watchtower':
        # Only an explicit false in BOTH the Compose config and the running container
        # proves isolation; a missing label is followed by Watchtower without label-enable.
        config = json.loads(compose('config', '--format', 'json'))
        if str((config['services']['odin'].get('labels') or {}).get(LABEL)).lower() != 'false':
            return 3
        running = inspect()
        return 0 if str((running['Config'].get('Labels') or {}).get(LABEL)).lower() == 'false' else 3
    if action == 'check':
        check()
        return
    if action == 'migrate':
        owned_override()
        old_id = inspect()['Image']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', old_id):
            raise RuntimeError('invalid running image ID')
        reference = repo_reference(old_id)
        rollback_file(reference, token)
        temporary = override_file(reference)
        try:
            # Recreate the SAME local image; migration must not pull a newer latest.
            compose('up', '-d', '--no-deps', '--pull', 'never', 'odin', override=temporary)
            running = inspect()
            if running['Image'] != old_id or running['Config']['Image'] != reference:
                raise RuntimeError('migration changed the running image')
            pin_override(reference)
            check()
        finally:
            os.unlink(temporary)
        return
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('invalid target digest')
    check()
    owned_override()
    reference = f'{IMAGE}@{digest}'
    temporary = override_file(reference)
    try:
        if action == 'pull':
            compose('pull', 'odin', override=temporary)
        else:
            raise ValueError('unknown action')
    finally:
        os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'watchtower', 'migrate', 'pull', 'apply', 'rollback'))
    parser.add_argument('--digest', default='')
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    try:
        return perform(args.action, args.digest, args.token) or 0
    except ApplyRefused:
        print('instance-update: apply refused before restart; running instance unchanged', file=sys.stderr)
        return APPLY_REFUSED
    except subprocess.TimeoutExpired:
        print('instance-update: command timed out; retain deploy protection', file=sys.stderr)
        return 124


if __name__ == '__main__':
    sys.exit(main())
