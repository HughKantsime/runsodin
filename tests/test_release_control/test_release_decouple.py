"""Offline contracts: no Docker daemon, SSH, registry or GitHub access."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from ops.release_control import checker, instance_update, mutation_workflow as mutation
from test_mutation_workflows import _publication_state

ROOT = Path(__file__).resolve().parents[2]
EXTERNAL = Path('/Users/ollama/Documents/Claude/odin/.claude')
DIGEST = 'sha256:' + 'c' * 64
OLD = 'sha256:' + 'd' * 64
NAME = 'odin-checker-400-1'
VERSION = '1.9.13'
PLATFORMS = ('linux/amd64', 'linux/arm64')
PLATFORM_NAMES = [NAME + '-' + platform.replace('/', '-') for platform in PLATFORMS]
# Real Docker (containerd store) reports the index digest as .Image for every variant.
IMAGE_IDS = {platform: DIGEST for platform in PLATFORMS}


def response(url):
    path = url.split(':43210')[-1]
    bodies = {
        '/health': {'status': 'ok', 'version': VERSION},
        '/health/ready': {'ready': True, 'version': VERSION},
        '/api/version': {'version': VERSION},
        '/api/setup/status': {'needs_setup': True, 'has_users': False, 'is_complete': False},
    }
    if path in bodies:
        return 'application/json', json.dumps(bodies[path]).encode()
    assert path in ('/login', '/setup')
    return 'text/html', b'<div id="root"></div><script src="/assets/app.js"></script>'


class Docker:
    def __init__(self, failure=None):
        self.commands = []
        self.failure = failure
        self.platform = None
        self.machine = None

    def __call__(self, *args):
        self.commands.append(args)
        if args[0] == self.failure:
            raise checker.CheckerError('injected Docker failure')
        if args[0] == 'run':
            self.platform = args[args.index('--platform') + 1]
        if args[0] == 'inspect':
            return json.dumps([{'Config': {'Image': checker.IMAGE + '@' + DIGEST},
                                'Image': IMAGE_IDS[self.platform], 'RestartCount': 0, 'State': {'Running': True, 'Health': {'Status': 'healthy'}},
                                'NetworkSettings': {'Ports': {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '43210'}]}}}])
        if args[0] == 'exec':
            # The running container is the only reliable witness of the variant:
            # with the containerd store, .Image is the index for every platform.
            if args[2:] == ('supervisorctl', 'status'):
                return 'api RUNNING pid 10, uptime 0:01:00\nmonitor RUNNING pid 11, uptime 0:01:00'
            assert args[2:] == ('uname', '-m')
            return self.machine or checker.MACHINE[self.platform]
        return ''


def run(docker, **kwargs):
    return checker.run_checker(DIGEST, VERSION, NAME, 'mac-mini', call=docker,
                               get=kwargs.pop('get', response), check_capacity=lambda: None,
                               timeout=kwargs.pop('timeout', 0), sleep=kwargs.pop('sleep', lambda _: None), **kwargs)


def test_checker_exact_digest_fresh_volume_loopback_and_cleanup():
    docker = Docker()
    requests = []
    def get(url):
        requests.append((docker.platform, url.split(':43210')[-1]))
        return response(url)
    evidence = run(docker, get=get)
    runs = [cmd for cmd in docker.commands if cmd[0] == 'run']
    assert len(runs) == 2
    for platform, name, cmd in zip(PLATFORMS, PLATFORM_NAMES, runs):
        assert cmd[cmd.index('--platform') + 1] == platform
        assert cmd[cmd.index('--name') + 1] == name
        assert '--pull=always' in cmd and cmd[-1] == checker.IMAGE + '@' + DIGEST
        assert cmd[cmd.index('--publish') + 1] == '127.0.0.1::8000'
        assert 'type=volume,source=' + name + '-data,target=/data' in cmd
        assert '-e' not in cmd  # no seeded admin: setup must actually be fresh
        create = docker.commands.index(('volume', 'create', name + '-data'))
        cleanup = docker.commands.index(('rm', '--force', name))
        assert create < docker.commands.index(cmd) < cleanup
        assert docker.commands[cleanup + 1] == ('volume', 'rm', name + '-data')
        assert evidence['platforms'][platform]['image_id'] == IMAGE_IDS[platform]
        assert evidence['platforms'][platform] == {
            'status': 'ok', 'version': VERSION, 'ready': True, 'digest': DIGEST,
            'image_id': IMAGE_IDS[platform],
            'smoke': {'login': True, 'api_version': True, 'fresh_setup': True}}
    assert docker.commands.index(('volume', 'rm', PLATFORM_NAMES[0] + '-data')) < docker.commands.index(('volume', 'create', PLATFORM_NAMES[1] + '-data'))
    assert requests == [(platform, path) for platform in PLATFORMS for _ in range(2) for path in
                        ('/health', '/health/ready', '/api/version', '/api/setup/status', '/login', '/setup')]
    assert evidence['digest'] == DIGEST and evidence['runner'] == 'mac-mini'
    assert evidence['ready'] is True
    assert evidence['image_id'] == IMAGE_IDS['linux/amd64']
    assert evidence['smoke'] == {'login': True, 'api_version': True, 'fresh_setup': True}


@pytest.mark.parametrize('failure', ['run', 'inspect', 'exec', 'rm'])
def test_checker_cleans_on_docker_failure_and_refuses_cleanup_failure(failure):
    docker = Docker(failure)
    with pytest.raises(checker.CheckerError):
        run(docker)
    assert docker.commands[-2:] == [('volume', 'rm', PLATFORM_NAMES[0] + '-data'), ('image', 'rm', checker.IMAGE + '@' + DIGEST)]


@pytest.mark.parametrize('platform', PLATFORMS)
@pytest.mark.parametrize('wrong', ['other-variant', 'empty', 'unknown', 'stopped'])
def test_checker_verifies_actual_running_image_platform(platform, wrong):
    docker = Docker()
    def call(*args):
        result = docker(*args)
        if docker.platform == platform:
            if args[0] == 'exec':
                result = {'other-variant': 'aarch64' if platform.endswith('amd64') else 'x86_64',
                          'empty': '', 'unknown': 'armv7l'}.get(wrong, result)
            elif args[0] == 'inspect' and wrong == 'stopped':
                info = json.loads(result)
                info[0]['State']['Running'] = False
                result = json.dumps(info)
        return result
    with pytest.raises(checker.CheckerError, match='platform mismatch|not running'):
        run(call)
    name = NAME + '-' + platform.replace('/', '-')
    assert docker.commands[-3:] == [('rm', '--force', name), ('volume', 'rm', name + '-data'), ('image', 'rm', checker.IMAGE + '@' + DIGEST)]


@pytest.mark.parametrize('platform', PLATFORMS)
@pytest.mark.parametrize('resource', ['container', 'volume'])
def test_checker_refuses_existing_platform_resources(platform, resource):
    docker = Docker()
    name = NAME + '-' + platform.replace('/', '-')
    def call(*args):
        result = docker(*args)
        if args[:2] == (resource, 'ls') and any(name in arg for arg in args):
            return name
        return result
    with pytest.raises(checker.CheckerError, match='already exists'):
        run(call)
    assert ('volume', 'create', name + '-data') not in docker.commands
    assert ('rm', '--force', name) not in docker.commands
    assert ('volume', 'rm', name + '-data') not in docker.commands


@pytest.mark.parametrize('platform', PLATFORMS)
@pytest.mark.parametrize('failure', ['volume-create', 'volume-rm', 'smoke'])
def test_checker_platform_failures_block_success_and_attempt_cleanup(platform, failure):
    docker = Docker()
    name = NAME + '-' + platform.replace('/', '-')
    def call(*args):
        result = docker(*args)
        operation = 'create' if failure == 'volume-create' else 'rm'
        if failure.startswith('volume-') and args == ('volume', operation, name + '-data'):
            raise checker.CheckerError('volume failure')
        return result
    def get(url):
        if docker.platform == platform and failure == 'smoke':
            raise OSError('not ready')
        return response(url)
    with pytest.raises(checker.CheckerError):
        run(call, get=get)
    assert docker.commands[-3:] == [('rm', '--force', name), ('volume', 'rm', name + '-data'), ('image', 'rm', checker.IMAGE + '@' + DIGEST)]
    if platform == PLATFORMS[0]:
        assert ('volume', 'create', PLATFORM_NAMES[1] + '-data') not in docker.commands


@pytest.mark.parametrize('path,body', [
    ('/health', {'status': 'degraded', 'version': VERSION}),
    ('/health', {'status': 'ok', 'version': '0.0.1'}),
    ('/health/ready', {'ready': False, 'version': VERSION}),
    ('/health/ready', {'ready': True, 'version': '0.0.1'}),
    ('/api/version', {'version': '0.0.1'}),
    ('/api/setup/status', {'needs_setup': False, 'has_users': True, 'is_complete': True}),
    ('/login', None), ('/setup', None),
])
def test_checker_rejects_wrong_health_api_setup_and_pages(path, body):
    def get(url):
        if url.endswith(path):
            return 'application/json', json.dumps(body).encode()
        return response(url)
    docker = Docker()
    with pytest.raises(checker.CheckerError, match='timed out'):
        run(docker, get=get)
    assert docker.commands[-2:] == [('volume', 'rm', PLATFORM_NAMES[0] + '-data'), ('image', 'rm', checker.IMAGE + '@' + DIGEST)]


def test_checker_does_not_accept_perimeter_or_capacity_failure():
    def unauthorized(url):
        raise OSError('HTTP 401')
    with pytest.raises(checker.CheckerError, match='timed out'):
        run(Docker(), get=unauthorized)
    docker = Docker()
    def capacity():
        raise checker.CheckerError('low capacity')
    with pytest.raises(checker.CheckerError, match='capacity'):
        checker.run_checker(DIGEST, VERSION, NAME, 'mac', call=docker, check_capacity=capacity)
    assert not docker.commands


def receipt(schema=2):
    state = _publication_state()
    for field in ('staging_tag', 'sha_tag', 'version_tag', 'platform_manifests'):
        del state[field]
    state.update(schema_version=schema, kind='production', publication_run_id=300,
                 publication_receipt_sha256='f' * 64, prior_latest_digest=OLD,
                 rollback_tag='rollback-400-1', rollback_command='restore', observed_latest_digest=DIGEST,
                 tag_writes=[{'tag': 'rollback-400-1', 'before': None, 'after': OLD, 'status': 'written'},
                             {'tag': 'latest', 'before': OLD, 'after': DIGEST, 'status': 'written'}])
    if schema == 1:
        state.update(phase='production_observed', public_health={
            'url': 'https://odin.subsystem.app/health', 'tls_valid': True,
            'status': 'ok', 'version': VERSION, 'ready_observation': 'ready_true'})
    else:
        state['checker_health'] = run(Docker())
    return state


@pytest.mark.parametrize('schema', [1, 2])
def test_receipt_legacy_and_checker_render_and_validate(schema, tmp_path):
    state = receipt(schema)
    mutation.validate_receipt(state)
    output = tmp_path / 'receipt'
    mutation.render_receipt(state, output)
    assert hashlib.sha256((output / 'receipt.json').read_bytes()).hexdigest() == (output / 'receipt.sha256').read_text().strip()
    assert ('mac-mini' if schema == 2 else 'ready_true') in (output / 'index.html').read_text()
    if schema == 2:
        assert all(platform in (output / 'index.html').read_text() for platform in PLATFORMS)


@pytest.mark.parametrize('field,value', [('digest', OLD), ('ready', False), ('version', '0.0.1'),
                                       ('runner', ''), ('status', 'failed'),
                                       ('image_id', 'bad'), ('image_id', 'sha256:' + 'd' * 64),
                                       ('smoke', {'login': True, 'api_version': True, 'fresh_setup': False})])
def test_receipt_rejects_forged_checker(field, value):
    state = receipt()
    state['checker_health'][field] = value
    with pytest.raises(mutation.MutationError):
        mutation.validate_receipt(state)


@pytest.mark.parametrize('platform', PLATFORMS)
@pytest.mark.parametrize('field,value', [('digest', OLD), ('ready', False), ('version', '0.0.1'),
                                       ('status', 'failed'), ('image_id', 'bad'),
                                       ('smoke', {'login': True, 'api_version': True, 'fresh_setup': False})])
def test_receipt_rejects_forged_platform_result(platform, field, value):
    state = receipt()
    state['checker_health']['platforms'][platform][field] = value
    with pytest.raises(mutation.MutationError):
        mutation.validate_receipt(state)


@pytest.mark.parametrize('mode', ['amd64-only', 'arm64-only', 'none', 'extra', 'old-single-result'])
def test_schema_two_requires_exactly_both_platforms(mode):
    state = receipt()
    health = state['checker_health']
    if mode == 'amd64-only': del health['platforms']['linux/arm64']
    elif mode == 'arm64-only': del health['platforms']['linux/amd64']
    elif mode == 'none': health['platforms'] = {}
    elif mode == 'extra': health['platforms']['linux/386'] = health['platforms']['linux/amd64']
    else:
        health.update(health['platforms']['linux/amd64'])
        del health['platforms']
    with pytest.raises(mutation.MutationError):
        mutation.validate_receipt(state)


@pytest.mark.parametrize('mode', ['no-checker', 'mixed', 'unknown-schema', 'wrong-latest', 'no-rollback', 'no-cleanup'])
def test_receipt_rejects_missing_binding_and_mixed_schemas(mode):
    state = receipt()
    if mode == 'no-checker': del state['checker_health']
    elif mode == 'mixed': state['public_health'] = receipt(1)['public_health']
    elif mode == 'unknown-schema': state['schema_version'] = 3
    elif mode == 'wrong-latest': state['observed_latest_digest'] = OLD
    elif mode == 'no-rollback': state['tag_writes'].pop(0)
    elif mode == 'no-cleanup': state['cleanup']['auth_removed'] = False
    with pytest.raises(mutation.MutationError): mutation.validate_receipt(state)


def external(name):
    path = EXTERNAL / name
    if not path.exists(): pytest.skip('operator tooling is outside repository')
    return path


@pytest.mark.parametrize('schema', [1, 2])
@pytest.mark.parametrize('invalid', [False, True])
def test_github_release_receipt_reader_offline(schema, invalid, tmp_path):
    source = external('tools/github-release.sh').read_text()
    code = source.split("<<'EOF'\n", 1)[1].split('\nEOF', 1)[0]
    state = receipt(schema)
    if invalid:
        state['public_health' if schema == 1 else 'checker_health']['version'] = '0.0.1'
    raw = json.dumps(state).encode()
    path = tmp_path / 'receipt.json'; path.write_bytes(raw)
    digest = tmp_path / 'receipt.sha256'; digest.write_text(hashlib.sha256(raw).hexdigest())
    result = subprocess.run([sys.executable, '-', str(path), str(digest), '400'], input=code, text=True, capture_output=True)
    assert (result.returncode != 0) == invalid, result.stderr
    assert 'live_version' in source and 'https://odin.subsystem.app/health' in source


def test_workflow_closure_includes_checker_and_capacity_probe_and_orders_mutation():
    source = (ROOT / '.github/workflows/promote-production.yml').read_text()
    steps = yaml.safe_load(source)['jobs']['promote']['steps']
    names = [s['name'] for s in steps]
    assert names.index('Remove disposable checker') < names.index('Create durable rollback tag') < names.index('Move latest to exact published digest')
    for filename in ('checker.py', 'validation_image_cleanup.py'):
        assert source.count('ops/release_control/' + filename) == 2
    assert 'public_health' not in source and 'odin.subsystem.app' not in source
    assert 'image prune' not in source


@pytest.mark.parametrize('step_name', ['Check exact published digest on disposable instance', 'Remove disposable checker'])
@pytest.mark.parametrize('leftover', ['', 'linux-amd64', 'linux-arm64', 'image'])
def test_workflow_cleanup_removes_both_platform_resources(step_name, leftover, tmp_path):
    steps = yaml.safe_load((ROOT / '.github/workflows/promote-production.yml').read_text())['jobs']['promote']['steps']
    step = next(step for step in steps if step['name'] == step_name)
    log = tmp_path / 'docker.log'
    harness = r'''
docker() {
  printf '%s\n' "$*" >> "$DOCKER_LOG"
  if [[ "$1 $2" == 'image rm' && "$LEFTOVER" == image ]]; then return 19; fi
  if [[ "$1 $2" == 'container ls' && -n "$LEFTOVER" && "$*" == *"$LEFTOVER"* ]]; then
    echo leftover
  fi
}
python3.11() { return 19; }
'''
    result = subprocess.run(['/bin/bash', '-c', harness + step['run']], text=True, capture_output=True,
                            env={**os.environ, 'CHECKER_NAME': NAME, 'MUTATION_ROOT': str(tmp_path),
                                 'HELPER_ROOT': str(tmp_path), 'INPUT_IMAGE_DIGEST': DIGEST,
                                 'INPUT_VERSION': VERSION, 'RUNNER_NAME': 'mac-mini', 'IMAGE_REPOSITORY': checker.IMAGE,
                                 'DOCKER_LOG': str(log), 'LEFTOVER': leftover})
    commands = log.read_text().splitlines()
    assert 'image rm ' + checker.IMAGE + '@' + DIGEST in commands
    assert not any('prune' in command for command in commands)
    for name in PLATFORM_NAMES:
        assert 'rm -f ' + name in commands
        assert 'volume rm ' + name + '-data' in commands
    if step_name == 'Remove disposable checker':
        assert step['if'] == 'always()'
        assert (result.returncode != 0) == bool(leftover)
    else:
        assert result.returncode == 19  # EXIT trap preserves checker failure


def test_chain_customer_release_precedes_host_gate(tmp_path):
    script = external('release-chain-v4.sh')
    helper = tmp_path / 'ops/release_control'; helper.mkdir(parents=True)
    (helper / 'instance_update.py').write_text('')
    log = tmp_path / 'chain.log'
    # Exercise main with completed validation/publication, replacing every external action.
    harness = r'''
source "$1"
CHAIN_LOG="$3"; VR=100; PB=300
lock_acquire(){ :; }; cleanup(){ :; }; verify_decoupled_main(){ :; }
git(){ case "$*" in *rev-parse*) printf '%040d\n' 1;; esac; }
python3.11(){ :; }; ci_disk(){ :; }; wait_validation(){ :; }; waitok(){ PP_COMPLETE=1; PP_CONCLUSION=success; }
download_run(){ echo '{}' > "$2/manifest.json"; echo '{}' > "$2/receipt.json"; }
publication_digest(){ echo sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc; }
instance_remote(){ echo "HOST $1"; [ "$1" = watchtower ]; }
run_production_eligibility(){ PE=200; echo ELIGIBILITY; }
dispatch_production(){ PP=400; echo PROMOTE; }
approve(){ echo APPROVE; }; move_main(){ MAIN_MOVED=1; }
bounded(){ echo GITHUB_RELEASE; }
idle_fleet(){ echo 'busy printers-running'; return 1; }
update_instance(){ echo "INSTANCE_AFTER_CUSTOMER=$CUSTOMER_RELEASE"; idle_fleet || :; }
main "$2" 1.9.13 authorized
'''
    result = subprocess.run(['/bin/bash', '-c', harness, 'test', str(script), str(tmp_path), str(log)], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr + log.read_text()
    text = log.read_text()
    assert text.index('PROMOTE') < text.index('GITHUB_RELEASE') < text.index('INSTANCE_AFTER_CUSTOMER=success') < text.index('busy printers-running')


@pytest.mark.parametrize('failure', ['', 'pull', 'apply', 'health'])
def test_chain_host_update_rechecks_freeze_and_keeps_customer_success(failure, tmp_path):
    script = external('release-chain-v4.sh')
    harness = r'''
source "$1"
V=1.9.13; DG=digest; CUSTOMER_RELEASE=success
instance_window(){ echo WINDOW; promotion_ready; }
instance_remote(){ echo "$1"; [ "$1" != "$FAILURE" ]; }
promotion_ready(){ echo RECHECK_FREEZE_AND_IDLE; }
wait_for_instance(){ echo HEALTH; [ "$FAILURE" != health ]; }
window_close(){ echo CLOSE; }
update_instance || :
echo "OUTCOME=$CUSTOMER_RELEASE/$INSTANCE_UPDATE"
'''
    result = subprocess.run(['/bin/bash', '-c', harness, 'test', str(script)], env={**os.environ, 'FAILURE': failure}, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    output = result.stdout
    assert 'OUTCOME=success/' in output
    if failure != 'pull': assert output.index('pull') < output.index('RECHECK_FREEZE_AND_IDLE') < output.index('apply')
    else: assert 'RECHECK_FREEZE_AND_IDLE' not in output
    if not failure: assert 'OUTCOME=success/success' in output and 'CLOSE' in output
    else: assert 'FAILED' in output


@pytest.mark.parametrize('action', ['migrate', 'pull', 'apply'])
@pytest.mark.parametrize('pinned', [False, True])
def test_host_helper_changes_only_odin_and_pins_image(action, pinned, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(instance_update, 'DIRECTORY', tmp_path)
    old_reference = instance_update.IMAGE + '@' + OLD
    old_image_id = 'sha256:' + 'e' * 64
    base = {'image': instance_update.IMAGE + ':latest', 'labels': {instance_update.LABEL: 'true'}}
    running_image = old_reference if pinned else base['image']
    state = {'Config': {'Image': running_image, 'Labels': {instance_update.LABEL: 'true' if action == 'migrate' else 'false'}}, 'Image': old_image_id}
    commands = []
    def effective(args):
        service = copy.deepcopy(base)
        for i, arg in enumerate(args):
            if arg == '-f' and args[i + 1] != 'docker-compose.yml':
                overlay = json.loads(Path(args[i + 1]).read_text())['services']['odin']
                service.update(overlay)
        return service
    def command(*args):
        commands.append(args)
        if args[:2] == ('docker', 'inspect'):
            return json.dumps([state])
        if args[:3] == ('docker', 'image', 'inspect'):
            image_id = old_image_id if args[-1] == old_image_id else DIGEST
            reference = old_reference if image_id == old_image_id else instance_update.IMAGE + '@' + DIGEST
            return json.dumps([{'Id': image_id, 'RepoDigests': [reference]}])
        if 'config' in args:
            return json.dumps({'services': {'odin': effective(args)}})
        if 'up' in args:
            service = effective(args)
            state['Image'] = old_image_id if action == 'migrate' else DIGEST
            state['Config'] = {'Image': service['image'], 'Labels': service['labels']}
        return ''
    monkeypatch.setattr(instance_update, 'command', command)
    if action != 'migrate' or pinned:
        overlay = copy.deepcopy(instance_update.OVERRIDE)
        if pinned: overlay['services']['odin']['image'] = old_reference
        (tmp_path / 'docker-compose.override.yml').write_text(json.dumps(overlay))
    instance_update.perform(action, DIGEST, 'a' * 32)
    mutations = [cmd for cmd in commands if 'up' in cmd or 'pull' in cmd]
    assert len(mutations) == 1 and mutations[0][-1] == 'odin'
    if action == 'pull':
        assert 'up' not in mutations[0]
        assert not list(tmp_path.glob('.odin-rollback-*'))
    else:
        assert '--no-deps' in mutations[0] and '--pull' in mutations[0] and 'never' in mutations[0]
        rollback = list(tmp_path.glob('.odin-rollback-*'))
        assert len(rollback) == 1
        assert json.loads(rollback[0].read_text())['services']['odin']['image'] == old_reference
        assert 'Rollback (inside a safe deploy window)' in capsys.readouterr().out
        expected = old_reference if action == 'migrate' else instance_update.IMAGE + '@' + DIGEST
        assert json.loads((tmp_path / 'docker-compose.override.yml').read_text())['services']['odin']['image'] == expected
        instance_update.check()  # ordinary Compose now describes the running image
    assert not list(tmp_path.glob('.odin-release-*'))
    if action == 'migrate':
        assert state['Image'] == old_image_id


def test_host_migration_refuses_unknown_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(instance_update, 'DIRECTORY', tmp_path)
    path = tmp_path / 'docker-compose.override.yml'
    path.write_text('{"services":{"other":{}}}')
    with pytest.raises(RuntimeError, match='operator reconciliation'):
        instance_update.perform('migrate', '', 'a' * 32)
    assert path.read_text() == '{"services":{"other":{}}}'


def test_host_pull_failure_never_restarts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(instance_update, 'DIRECTORY', tmp_path)
    monkeypatch.setattr(instance_update, 'check', lambda: None)
    commands = []
    def compose(*args, **kwargs):
        commands.append(args)
        raise RuntimeError('pull failed')
    monkeypatch.setattr(instance_update, 'compose', compose)
    with pytest.raises(RuntimeError, match='pull failed'):
        instance_update.perform('pull', DIGEST, 'a' * 32)
    assert commands == [('pull', 'odin')]
    assert not list(tmp_path.glob('.odin-release-*'))


@pytest.mark.parametrize('workflow', ['promote-production.yml', 'rollback-2.0-production.yml'])
def test_changed_workflow_shell_blocks_parse(workflow):
    document = yaml.safe_load((ROOT / '.github/workflows' / workflow).read_text())
    for job in document['jobs'].values():
        for step in job['steps']:
            if 'run' in step:
                result = subprocess.run(['/bin/bash', '-n'], input=step['run'], text=True, capture_output=True)
                assert result.returncode == 0, step['name'] + result.stderr


@pytest.mark.parametrize('code', [124, 255])
def test_interrupted_host_restart_retains_protection(code):
    script = external('release-chain-v4.sh')
    harness = r'''
source "$1"
V=1.9.13; DG=digest; CUSTOMER_RELEASE=success
instance_window(){ :; }; promotion_ready(){ :; }
instance_remote(){ if [ "$1" = apply ]; then return "$CODE"; fi; }
update_instance || :
printf 'PENDING=%s CUSTOMER=%s\n' "$HOST_MUTATION_PENDING" "$CUSTOMER_RELEASE"
'''
    result = subprocess.run(['/bin/bash', '-c', harness, 'test', str(script)], env={**os.environ, 'CODE': str(code)}, text=True, capture_output=True)
    assert result.returncode == 0 and 'PENDING=1 CUSTOMER=success' in result.stdout


@pytest.mark.parametrize('which', ['runtime', 'compose'])
def test_watchtower_must_be_disabled_in_both_config_and_container(which, monkeypatch):
    monkeypatch.setattr(instance_update, 'inspect', lambda: {'Config': {'Labels': {instance_update.LABEL: 'true' if which == 'runtime' else 'false'}}})
    monkeypatch.setattr(instance_update, 'compose', lambda *args: json.dumps({'services': {'odin': {'labels': {instance_update.LABEL: 'true' if which == 'compose' else 'false'}}}}))
    with pytest.raises(RuntimeError, match='Watchtower'):
        instance_update.check()


@pytest.mark.parametrize('wrong', ['binding', 'digest'])
def test_checker_rejects_wrong_image_or_exposed_port(wrong):
    docker = Docker()
    def call(*args):
        result = docker(*args)
        if args[0] == 'inspect':
            info = json.loads(result)
            if wrong == 'binding': info[0]['NetworkSettings']['Ports']['8000/tcp'][0]['HostIp'] = '0.0.0.0'
            else: info[0]['Config']['Image'] = checker.IMAGE + ':latest'
            return json.dumps(info)
        return result
    with pytest.raises(checker.CheckerError): run(call)
    assert docker.commands[-2:] == [('volume', 'rm', PLATFORM_NAMES[0] + '-data'), ('image', 'rm', checker.IMAGE + '@' + DIGEST)]


def test_checker_refuses_a_container_running_the_wrong_variant():
    docker = Docker()
    docker.machine = 'aarch64'  # e.g. an amd64 request that silently ran the host variant
    with pytest.raises(checker.CheckerError, match='platform mismatch'):
        run(docker)
    assert any(cmd[:2] == ('rm', '--force') for cmd in docker.commands)


@pytest.mark.parametrize('failure_at', [0, 1, 2, 3])
@pytest.mark.parametrize('schema', [1, 2])
@pytest.mark.parametrize('failure_mode', ['network', 'mismatch'])
def test_github_release_live_guards_before_each_write(schema, failure_at, failure_mode, tmp_path):
    """Execute the whole helper; gh/curl are shell fakes, never real services."""
    state = receipt(schema)
    raw = json.dumps(state).encode()
    (tmp_path / 'receipt.json').write_bytes(raw)
    (tmp_path / 'receipt.sha256').write_text(hashlib.sha256(raw).hexdigest())
    source = external('tools/github-release.sh').read_text().replace(
        'LOCK="$HOME/.odin-github-release.lock"', 'LOCK="$FIXTURE/lock"')
    script = tmp_path / 'github-release.sh'; script.write_text(source)
    harness = r'''
gh() {
  case "$1 $2" in
    'run list') echo 400;;
    'run download')
      local prev='' arg dir=''
      for arg in "$@"; do [ "$prev" != -D ] || dir=$arg; prev=$arg; done
      cp "$FIXTURE/receipt.json" "$FIXTURE/receipt.sha256" "$dir/";;
    'api -H') case "$*" in *VERSION*) echo 1.9.13;; *CHANGELOG*) printf '## [1.9.13]\nFixes\n';; *) return 99;; esac;;
    'api -X') echo TAG >> "$FIXTURE/writes"; touch "$FIXTURE/tag";;
    'release create') echo RELEASE >> "$FIXTURE/writes"; touch "$FIXTURE/released"
      local prev='' arg
      for arg in "$@"; do [ "$prev" != --notes-file ] || cp "$arg" "$FIXTURE/notes"; prev=$arg; done;;
    'release view')
      if [ ! -e "$FIXTURE/released" ]; then echo 'release not found' >&2; return 1; fi
      case "$*" in *isDraft*) echo 'false false';; *body*) cat "$FIXTURE/notes";; *tagName*) echo v1.9.13;; *) return 99;; esac;;
    api*)
      case "$*" in *git/ref/tags*)
        if [ -e "$FIXTURE/tag" ]; then echo "commit $SHA_EXPECTED"; else echo 'HTTP 404' >&2; return 1; fi;;
        *) return 99;; esac;;
    *) return 99;;
  esac
}
curl() {
  case "$*" in
    *ghcr.io/token*) echo '{"token":"anonymous-test"}';;
    *manifests/v1.9.13*) echo "Docker-Content-Digest: $EXPECTED_DIGEST";;
    *odin.subsystem.app/health*|*manifests/latest*)
      if [ "$SCHEMA_TEST" = 1 ]; then [[ "$*" == *odin.subsystem.app/health* ]] || return 99
      else [[ "$*" == *manifests/latest* ]] || return 99; fi
      local count=0
      [ ! -f "$FIXTURE/count" ] || count=$(cat "$FIXTURE/count")
      count=$((count + 1)); echo "$count" > "$FIXTURE/count"
      if [ "$count" = "$FAILURE_AT" ]; then
        if [ "$FAILURE_MODE" = network ]; then return 22; fi
        if [ "$SCHEMA_TEST" = 1 ]; then echo '{"status":"ok","version":"0.0.1"}'
        else echo "Docker-Content-Digest: sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"; fi
        return 0
      fi
      if [ "$SCHEMA_TEST" = 1 ]; then echo '{"status":"ok","version":"1.9.13"}'
      else echo "Docker-Content-Digest: $EXPECTED_DIGEST"; fi;;
    *) return 99;;
  esac
}
source "$1" 1.9.13
'''
    result = subprocess.run(['/bin/bash', '-c', harness, 'test', str(script)], text=True,
                            capture_output=True, timeout=10, env={**os.environ,
                            'FIXTURE': str(tmp_path), 'EXPECTED_DIGEST': DIGEST,
                            'SHA_EXPECTED': state['candidate_sha'], 'SCHEMA_TEST': str(schema),
                            'FAILURE_AT': str(failure_at), 'FAILURE_MODE': failure_mode})
    assert (result.returncode == 0) == (failure_at == 0), result.stdout + result.stderr
    writes = (tmp_path / 'writes').read_text().splitlines() if (tmp_path / 'writes').exists() else []
    assert writes == ({0: ['TAG', 'RELEASE'], 1: [], 2: [], 3: ['TAG']}[failure_at])
    assert int((tmp_path / 'count').read_text()) == (failure_at or 3)


@pytest.mark.parametrize('mode', ['missing', 'bad-status', 'bad-image-id', 'wrong-version', 'wrong-digest', 'unready', 'bad-smoke', 'summary-mismatch'])
def test_release_helper_rejects_bad_platform_evidence(mode, tmp_path):
    source = external('tools/github-release.sh').read_text()
    code = source.split("<<'EOF'\n", 1)[1].split('\nEOF', 1)[0]
    state = receipt()
    health = state['checker_health']
    if mode == 'missing': del health['platforms']['linux/arm64']
    elif mode == 'summary-mismatch': health['image_id'] = OLD
    else:
        field, value = {'bad-status': ('status', 'failed'), 'bad-image-id': ('image_id', 'bad'),
                        'wrong-version': ('version', '0.0.1'), 'wrong-digest': ('digest', OLD),
                        'unready': ('ready', False), 'bad-smoke': ('smoke', {})}[mode]
        health['platforms']['linux/arm64'][field] = value
    raw = json.dumps(state).encode()
    path = tmp_path / 'receipt.json'; path.write_bytes(raw)
    hash_path = tmp_path / 'receipt.sha256'; hash_path.write_text(hashlib.sha256(raw).hexdigest())
    result = subprocess.run([sys.executable, '-', str(path), str(hash_path), '400'], input=code, text=True, capture_output=True)
    assert result.returncode != 0 and 'health evidence' in result.stderr


@pytest.mark.parametrize('recovers', [False, True])
def test_checker_retry_with_nonzero_timeout_and_fake_clock(recovers):
    now = [0]
    sleeps = []
    attempts = []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    def get(url):
        if url.endswith('/health'):
            attempts.append(now[0])
            if not recovers or now[0] < 10: raise OSError('starting')
        return response(url)
    docker = Docker()
    if recovers:
        evidence = run(docker, get=get, timeout=12, clock=lambda: now[0], sleep=sleep)
        assert evidence['ready'] is True
        assert sleeps == [5, 5, 45, 45]
        assert attempts[:3] == [0, 5, 10]
    else:
        with pytest.raises(checker.CheckerError, match='timed out'):
            run(docker, get=get, timeout=12, clock=lambda: now[0], sleep=sleep)
        assert attempts == [0, 5, 10, 15]
        assert docker.commands[-1] == ('image', 'rm', checker.IMAGE + '@' + DIGEST)


@pytest.mark.parametrize('failure', ['index', 'stopped', 'restart', 'unhealthy', 'supervisor', 'no-processes', 'second-smoke', 'image-cleanup'])
def test_checker_stability_and_exact_cleanup_fail_closed(failure):
    docker = Docker()
    inspections = [0]
    def call(*args):
        value = docker(*args)
        if args[0] == 'inspect':
            inspections[0] += 1
            info = json.loads(value)
            if failure == 'index': info[0]['Image'] = OLD
            if inspections[0] == 2:
                if failure == 'stopped': info[0]['State']['Running'] = False
                if failure == 'restart': info[0]['RestartCount'] = 1
                if failure == 'unhealthy': info[0]['State']['Health']['Status'] = 'unhealthy'
            value = json.dumps(info)
        if args[0] == 'exec' and args[2:] == ('supervisorctl', 'status'):
            if failure == 'supervisor': return 'api RUNNING\nmonitor BACKOFF'
            if failure == 'no-processes': return ''
        if args[:2] == ('image', 'rm') and failure == 'image-cleanup':
            raise checker.CheckerError('image cleanup failed')
        return value
    def get(url):
        if failure == 'second-smoke' and inspections[0] >= 2: raise checker.CheckerError('second smoke failed')
        return response(url)
    with pytest.raises(checker.CheckerError): run(call, get=get)
    assert docker.commands[-1] == ('image', 'rm', checker.IMAGE + '@' + DIGEST)
    assert not any('prune' in cmd for cmd in docker.commands)


@pytest.mark.parametrize('action', ['migrate', 'pull', 'apply'])
def test_instance_helper_timeout_exits_124_without_command_output(action, tmp_path):
    # Run the actual CLI entry point in a subprocess, replacing only its operation.
    code = '''import runpy, subprocess, sys
namespace = runpy.run_module('ops.release_control.instance_update', run_name='timeout_test')
def timeout(*args):
    raise subprocess.TimeoutExpired('sensitive-command', 30, output='secret-output')
namespace['main'].__globals__['perform'] = timeout
sys.exit(namespace['main']())
'''
    result = subprocess.run([sys.executable, '-c', code, action, '--token', 'a' * 32], capture_output=True, text=True)
    assert result.returncode == 124
    assert 'secret' not in result.stderr and 'sensitive' not in result.stderr
    assert 'retain deploy protection' in result.stderr


def test_instance_check_rejects_compose_image_drift(monkeypatch):
    monkeypatch.setattr(instance_update, 'compose', lambda *args: json.dumps({'services': {'odin': {
        'image': instance_update.IMAGE + ':latest', 'labels': {instance_update.LABEL: 'false'}}}}))
    monkeypatch.setattr(instance_update, 'inspect', lambda: {'Config': {
        'Image': instance_update.IMAGE + '@' + DIGEST, 'Labels': {instance_update.LABEL: 'false'}}})
    with pytest.raises(RuntimeError, match='Compose image differs'): instance_update.check()


@pytest.mark.parametrize('code', [0, 3, 124, 255])
def test_chain_pre_release_host_check_only_blocks_enabled_watchtower(code):
    harness = r'''
source "$1"
instance_remote(){ [ "$1" = watchtower ] || return 99; return "$CODE"; }
pre_release_host_check
'''
    result = subprocess.run(['/bin/bash', '-c', harness, 'test', str(external('release-chain-v4.sh'))],
                            text=True, capture_output=True, env={**os.environ, 'CODE': str(code)})
    assert (result.returncode != 0) == (code == 3)
    if code in (124, 255): assert 'WARNING' in result.stdout


@pytest.mark.parametrize('which', ['compose', 'runtime', 'neither'])
def test_watchtower_probe_reads_both_labels(which, tmp_path, monkeypatch):
    monkeypatch.setattr(instance_update, 'DIRECTORY', tmp_path)
    monkeypatch.setattr(instance_update, 'compose', lambda *args: json.dumps({'services': {'odin': {
        'labels': {instance_update.LABEL: str(which == 'compose').lower()}}}}))
    monkeypatch.setattr(instance_update, 'inspect', lambda: {'Config': {
        'Labels': {instance_update.LABEL: str(which == 'runtime').lower()}}})
    monkeypatch.chdir(tmp_path)
    assert instance_update.perform('watchtower', '', 'a' * 32) == (0 if which == 'neither' else 3)


@pytest.mark.parametrize('workflow', ['legacy', 'decoupled'])
def test_chain_refuses_old_workflow_on_main(workflow):
    harness = r'''
source "$1"
gh_call(){
  [[ "$*" == *'?ref=main'* ]] || return 99
  if [ "$WORKFLOW" = legacy ]; then echo PUBLIC_HEALTH_URL
  else printf 'ops.release_control.checker\nchecker_health\n'; fi
}
verify_decoupled_main
'''
    result = subprocess.run(['/bin/bash', '-c', harness, 'test', str(external('release-chain-v4.sh'))],
                            text=True, capture_output=True, env={**os.environ, 'WORKFLOW': workflow})
    assert (result.returncode == 0) == (workflow == 'decoupled')


def test_chain_update_instance_only_never_enters_release_path(tmp_path):
    helper = tmp_path / 'ops/release_control'; helper.mkdir(parents=True)
    (helper / 'instance_update.py').write_text('')
    log = tmp_path / 'chain.log'
    harness = r'''
source "$1"
CHAIN_LOG="$3"
lock_acquire(){ echo LOCK; }; cleanup(){ :; }
verify_decoupled_main(){ echo FORBIDDEN; return 99; }
gh_call(){ echo FORBIDDEN; return 99; }; git(){ echo FORBIDDEN; return 99; }
instance_remote(){ echo "HOST $1"; }
update_instance(){ echo "UPDATE $DG CUSTOMER=$CUSTOMER_RELEASE"; }
main "$2" 1.9.13 authorized --update-instance-only "$4"
'''
    result = subprocess.run(['/bin/bash', '-c', harness, 'test', str(external('release-chain-v4.sh')), str(tmp_path), str(log), DIGEST], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    output = log.read_text()
    assert 'HOST check' in output and f'UPDATE {DIGEST} CUSTOMER=unchanged' in output
    assert 'FORBIDDEN' not in output


def test_chain_bounded_wait_defers_and_releases_lock(tmp_path):
    harness = r'''
source "$1"
WT=/checkout; V=1.9.13; AUTH=authorized; DG=digest; CUSTOMER_RELEASE=success
NOW=0
date(){ if [ "$1" = +%s ]; then echo "$NOW"; else echo clock; fi; }
pause_for(){ NOW=$((NOW + $1)); }
freeze_check(){ :; }; idle_fleet(){ echo busy; }
instance_remote(){ echo "HOST $1"; }
open_window(){ echo FORBIDDEN_WINDOW; return 99; }
window_close(){ echo CLOSE; }; lock_release(){ echo UNLOCK; }; trim_colima(){ :; }
update_instance
printf 'RESULT=%s ELAPSED=%s\n' "$INSTANCE_UPDATE" "$NOW"
cleanup 0
'''
    result = subprocess.run(['/bin/bash', '-c', harness, 'test', str(external('release-chain-v4.sh'))], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'RESULT=deferred ELAPSED=86400' in result.stdout
    assert '--update-instance-only' in result.stdout and 'UNLOCK' in result.stdout
    assert 'FORBIDDEN_WINDOW' not in result.stdout and 'HOST apply' not in result.stdout


def test_chain_health_failure_keeps_marker_and_prints_rollback():
    harness = r'''
source "$1"
V=1.9.13; DG=digest; CUSTOMER_RELEASE=success; TOKEN=abc
instance_remote(){ echo "$1"; }; instance_window(){ echo WINDOW; }; promotion_ready(){ :; }
wait_for_instance(){ return 1; }
window_close(){ echo FORBIDDEN_CLOSE; }; lock_release(){ echo FORBIDDEN_UNLOCK; }
report_exit(){ :; }
update_instance || :
echo "HOLD=$HOLD"
cleanup 1
'''
    result = subprocess.run(['/bin/bash', '-c', harness, 'test', str(external('release-chain-v4.sh'))], text=True, capture_output=True)
    assert result.returncode == 1
    assert result.stdout.index('pull') < result.stdout.index('WINDOW') < result.stdout.index('apply')
    assert 'HOLD=1' in result.stdout and '.odin-rollback-abc.json' in result.stdout
    assert 'FORBIDDEN' not in result.stdout


@pytest.mark.parametrize('invalid', ['digest', 'version', 'platforms', 'image_id', 'ready', 'valid'])
def test_checker_evidence_validated_before_mutation(invalid, tmp_path):
    steps = yaml.safe_load((ROOT / '.github/workflows/promote-production.yml').read_text())['jobs']['promote']['steps']
    source = next(step['run'] for step in steps if step['name'] == 'Create durable rollback tag')
    validation = source.split("<<'PY'\n", 1)[1].split('\nPY', 1)[0]
    assert source.index('validate_evidence') < source.index('MUTATION_STARTED=1') < source.index('attach-tag')
    evidence = run(Docker())
    if invalid == 'digest': evidence['digest'] = OLD
    elif invalid == 'version': evidence['version'] = '0.0.1'
    elif invalid == 'platforms': del evidence['platforms']['linux/arm64']
    elif invalid == 'image_id': evidence['platforms']['linux/arm64']['image_id'] = OLD
    elif invalid == 'ready': evidence['platforms']['linux/amd64']['ready'] = False
    (tmp_path / 'checker.json').write_text(json.dumps(evidence))
    result = subprocess.run([sys.executable, '-c', validation], text=True, capture_output=True,
                            env={**os.environ, 'MUTATION_ROOT': str(tmp_path), 'INPUT_IMAGE_DIGEST': DIGEST, 'INPUT_VERSION': VERSION})
    assert (result.returncode == 0) == (invalid == 'valid'), result.stderr


@pytest.mark.parametrize('replace_fails', [False, True])
def test_instance_pin_is_atomic_and_retains_owned_content_on_failure(replace_fails, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'docker-compose.override.yml'
    original = json.dumps(instance_update.OVERRIDE)
    path.write_text(original)
    real_replace = os.replace
    replacements = []
    def replace(source, destination):
        replacements.append((source, destination))
        assert path.read_text() == original
        assert Path(source).stat().st_mode & 0o777 == 0o600
        if replace_fails: raise OSError('injected atomic replacement failure')
        real_replace(source, destination)
    monkeypatch.setattr(instance_update.os, 'replace', replace)
    if replace_fails:
        with pytest.raises(OSError): instance_update.pin_override(instance_update.IMAGE + '@' + DIGEST)
        assert path.read_text() == original
    else:
        instance_update.pin_override(instance_update.IMAGE + '@' + DIGEST)
        assert json.loads(path.read_text())['services']['odin']['image'] == instance_update.IMAGE + '@' + DIGEST
    assert len(replacements) == 1
    assert not list(tmp_path.glob('.odin-release-*'))


def test_chain_fleet_recheck_inside_window_shares_24_hour_deadline():
    harness = r'''
source "$1"
WT=/checkout; V=1.9.13; DG=digest; CUSTOMER_RELEASE=success; NOW=0
date(){ if [ "$1" = +%s ]; then echo "$NOW"; else echo clock; fi; }
freeze_check(){ :; }; idle_fleet(){ echo idle; }
instance_remote(){ echo "HOST $1"; }
open_window(){ echo WINDOW; }
promotion_ready(){
  [ "$INSTANCE_DEADLINE" = 86400 ] || return 99
  NOW=86400
  time_left
}
window_close(){ echo CLOSE; }; lock_release(){ echo UNLOCK; }; trim_colima(){ :; }
update_instance
echo "RESULT=$INSTANCE_UPDATE"
cleanup 0
'''
    result = subprocess.run(['/bin/bash', '-c', harness, 'test', str(external('release-chain-v4.sh'))], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'RESULT=deferred' in result.stdout and 'UNLOCK' in result.stdout and 'CLOSE' in result.stdout
    assert 'HOST apply' not in result.stdout
