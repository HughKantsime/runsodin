"""The odin-ci host scripts under ops/ci, always behind stub tools: nothing here touches a real
runner, service, firewall or process."""
import json
import re
import shutil
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
CI = ROOT / 'ops/ci'
HOOK = CI / 'runner-job-started-hook.sh'
LISTENER = '/home/odin/actions-runner-runsodin/bin/Runner.Listener run --startuptype service'
WORKER = '/home/odin/actions-runner-runsodin/bin/Runner.Worker spawnclient 124 127'


def _stub(directory, name, body):
    path = directory / name
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)


def _base_env(path):
    return {'PATH': f'{path}:/usr/bin:/bin', 'HOME': str(path.parent)}


@pytest.fixture
def stubs(tmp_path):
    directory = tmp_path / 'stubs'
    directory.mkdir()
    # Any command these scripts would run with privileges lands here instead.
    _stub(directory, 'sudo', f'echo "$*" >> {tmp_path}/sudo.log')
    return directory


# ---------------- job-started hook ----------------

def _event(**changes):
    event = {'sender': {'login': 'HughKantsime', 'id': 201174638},
             'repository': {'full_name': 'HughKantsime/runsodin', 'id': 1143865575}, 'inputs': {}}
    event.update(changes)
    return event


def _run_hook(tmp_path, event=None, drop=(), **env):
    payload = tmp_path / 'event.json'
    payload.write_text(json.dumps(_event() if event is None else event))
    jq = shutil.which('jq')
    assert jq, 'jq is required by the hook'
    values = {
        'PATH': f'{Path(jq).parent}:/usr/bin:/bin', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_REPOSITORY': 'HughKantsime/runsodin', 'GITHUB_REPOSITORY_ID': '1143865575',
        'GITHUB_ACTOR': 'HughKantsime', 'GITHUB_ACTOR_ID': '201174638',
        'GITHUB_TRIGGERING_ACTOR': 'HughKantsime', 'GITHUB_EVENT_PATH': str(payload), **env,
    }
    for name in drop:
        del values[name]
    return subprocess.run(['/bin/bash', str(HOOK)], env=values, capture_output=True, text=True)


def test_hook_accepts_an_owner_dispatch_on_this_repository(tmp_path):
    result = _run_hook(tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('event', ['pull_request', 'pull_request_target', 'pull_request_review', 'push', 'schedule',
                                   'workflow_run', 'workflow_call', 'repository_dispatch', 'issue_comment', ''])
def test_hook_refuses_every_other_event(tmp_path, event):
    result = _run_hook(tmp_path, GITHUB_EVENT_NAME=event)
    assert result.returncode != 0 and 'refuses' in result.stderr


@pytest.mark.parametrize('name,value', [
    ('GITHUB_ACTOR', 'returning-contributor'), ('GITHUB_ACTOR_ID', '1'), ('GITHUB_TRIGGERING_ACTOR', 'someone-else'),
    ('GITHUB_REPOSITORY', 'someone-else/runsodin'), ('GITHUB_REPOSITORY_ID', '2'),
])
def test_hook_refuses_anyone_or_anywhere_else(tmp_path, name, value):
    result = _run_hook(tmp_path, **{name: value})
    assert result.returncode != 0 and 'refuses' in result.stderr


@pytest.mark.parametrize('name', [
    'GITHUB_EVENT_NAME', 'GITHUB_ACTOR', 'GITHUB_ACTOR_ID', 'GITHUB_TRIGGERING_ACTOR',
    'GITHUB_REPOSITORY', 'GITHUB_REPOSITORY_ID', 'GITHUB_EVENT_PATH',
])
def test_hook_refuses_when_a_fact_is_missing(tmp_path, name):
    result = _run_hook(tmp_path, drop=(name,))
    assert result.returncode != 0 and 'refuses' in result.stderr


@pytest.mark.parametrize('change', [
    {'sender': {'login': 'returning-contributor', 'id': 1}},
    {'sender': {'login': 'HughKantsime', 'id': 1}},
    {'repository': {'full_name': 'someone-else/runsodin', 'id': 1143865575}},
    {'repository': {'full_name': 'HughKantsime/runsodin', 'id': 2}},
    {'pull_request': {'number': 1}},
])
def test_hook_refuses_a_payload_that_disagrees_with_the_environment(tmp_path, change):
    result = _run_hook(tmp_path, event=_event(**change))
    assert result.returncode != 0 and 'event payload' in result.stderr


@pytest.mark.parametrize('content', ['not json', '{}', 'null'])
def test_hook_refuses_an_unreadable_payload(tmp_path, content):
    payload = tmp_path / 'broken.json'
    payload.write_text(content)
    result = _run_hook(tmp_path, GITHUB_EVENT_PATH=str(payload))
    assert result.returncode != 0 and 'event payload' in result.stderr


def test_hook_fails_closed_without_jq(tmp_path):
    empty = tmp_path / 'no-tools'
    empty.mkdir()
    result = _run_hook(tmp_path, PATH=str(empty))
    assert result.returncode != 0 and 'refuses' in result.stderr


def test_hook_identity_matches_the_workflows_owner_pin():
    source = HOOK.read_text()
    workflow = (ROOT / '.github/workflows/trusted-validation.yml').read_text()
    for pin in ('HughKantsime', '201174638'):
        assert pin in source and pin in workflow


# ---------------- runner .env and restart ----------------

def _lib(body, tmp_path, stubs, **env):
    script = f'set -euo pipefail\n. {CI}/runner-lib.sh\n{body}\n'
    return subprocess.run(['/bin/bash', '-c', script], env={**_base_env(stubs), 'RUNNER_DIR': str(tmp_path), **env},
                          capture_output=True, text=True)


def _ps(stubs, *lines):
    listing = stubs.parent / 'ps.txt'
    listing.write_text(''.join(line + '\n' for line in lines))
    _stub(stubs, 'ps', f'cat {listing}')


def test_env_set_updates_an_old_value_keeps_the_rest_and_reports_no_change_when_right(tmp_path, stubs):
    env_file = tmp_path / '.env'
    env_file.write_text('LANG=C.UTF-8\nTMPDIR=/home/odin/old\n')
    changed = _lib('runner_env_set TMPDIR /opt/odin-ci-tmp', tmp_path, stubs)
    assert changed.returncode == 0, changed.stderr
    assert env_file.read_text() == 'LANG=C.UTF-8\nTMPDIR=/opt/odin-ci-tmp\n'
    assert _lib('runner_env_set TMPDIR /opt/odin-ci-tmp', tmp_path, stubs).returncode == 1
    assert env_file.read_text() == 'LANG=C.UTF-8\nTMPDIR=/opt/odin-ci-tmp\n'


def test_env_set_creates_the_file_and_collapses_duplicates(tmp_path, stubs):
    env_file = tmp_path / '.env'
    assert _lib('runner_env_set KEY one', tmp_path, stubs).returncode == 0
    assert env_file.read_text() == 'KEY=one\n'
    env_file.write_text('KEY=one\nOTHER=x\nKEY=one\n')
    assert _lib('runner_env_set KEY one', tmp_path, stubs).returncode == 0
    assert env_file.read_text() == 'OTHER=x\nKEY=one\n'


def test_restart_runs_the_registered_unit(tmp_path, stubs):
    (tmp_path / '.service').write_text('actions.runner.synthetic.service\n')
    _ps(stubs, LISTENER)
    assert _lib('runner_restart', tmp_path, stubs).returncode == 0
    assert (stubs.parent / 'sudo.log').read_text() == 'systemctl restart actions.runner.synthetic.service\n'


def test_restart_refuses_during_a_job(tmp_path, stubs):
    (tmp_path / '.service').write_text('actions.runner.synthetic.service\n')
    _ps(stubs, LISTENER, WORKER)
    result = _lib('runner_restart', tmp_path, stubs)
    assert result.returncode == 1 and 'systemctl restart' in result.stderr
    assert not (stubs.parent / 'sudo.log').exists()


def test_restart_before_registration_touches_nothing(tmp_path, stubs):
    _ps(stubs, LISTENER)
    assert _lib('runner_restart', tmp_path, stubs).returncode == 0
    assert not (stubs.parent / 'sudo.log').exists()


def test_tools_provisioning_replaces_an_old_tmpdir_and_restarts_after_a_change():
    source = (CI / 'provision-odin-ci-tools.sh').read_text()
    assert 'if runner_env_set TMPDIR "$ci_tmp"; then\n  runner_restart\nfi' in source
    assert "grep -qs '^TMPDIR='" not in source


def test_provisioning_sets_the_hostname_the_limits_and_phase0_rely_on():
    assert 'sudo hostnamectl set-hostname odin-ci' in (CI / 'provision-odin-ci.sh').read_text()
    thresholds = json.loads((ROOT / 'ops/edu_readiness/thresholds.json').read_text())
    assert thresholds['host_classes']['odin-ci']['identity']['hostname'] == 'odin-ci'
    assert '"$hostname" == "odin-ci"' in (ROOT / 'ops/phase0_verify.sh').read_text()


# ---------------- hook installer ----------------

def _install(tmp_path, stubs):
    runner = tmp_path / 'runner'
    runner.mkdir(exist_ok=True)
    _stub(stubs, 'jq', 'exit 0')
    return runner, subprocess.run(['/bin/bash', str(CI / 'install-runner-hook.sh')],
                                  env={**_base_env(stubs), 'RUNNER_DIR': str(runner)}, capture_output=True, text=True)


def test_installer_makes_the_hook_root_owned_points_env_at_it_and_restarts(tmp_path, stubs):
    runner = tmp_path / 'runner'
    runner.mkdir()
    (runner / '.env').write_text('LANG=C.UTF-8\n')
    (runner / '.service').write_text('actions.runner.synthetic.service\n')
    _ps(stubs, LISTENER)
    _, result = _install(tmp_path, stubs)
    assert result.returncode == 0, result.stderr
    hook = '/usr/local/libexec/odin-ci/runner-job-started-hook.sh'
    assert (runner / '.env').read_text() == f'LANG=C.UTF-8\nACTIONS_RUNNER_HOOK_JOB_STARTED={hook}\n'
    log = (tmp_path / 'sudo.log').read_text().splitlines()
    assert log == [
        'install -d -m 0755 -o root -g root /usr/local/libexec/odin-ci',
        f'install -m 0755 -o root -g root {HOOK} {hook}',
        'systemctl restart actions.runner.synthetic.service',
    ]


def test_installer_does_not_restart_when_env_is_already_right(tmp_path, stubs):
    runner = tmp_path / 'runner'
    runner.mkdir()
    (runner / '.env').write_text('ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/libexec/odin-ci/runner-job-started-hook.sh\n')
    (runner / '.service').write_text('actions.runner.synthetic.service\n')
    _ps(stubs, LISTENER)
    _, result = _install(tmp_path, stubs)
    assert result.returncode == 0, result.stderr
    assert 'systemctl' not in (tmp_path / 'sudo.log').read_text()


def test_installer_refuses_during_a_job(tmp_path, stubs):
    _ps(stubs, LISTENER, WORKER)
    runner, result = _install(tmp_path, stubs)
    assert result.returncode == 75
    assert not (tmp_path / 'sudo.log').exists() and not (runner / '.env').exists()


# ---------------- egress to production ----------------

def _egress(tmp_path, stubs, action, listing):
    (tmp_path / 'nft-list.txt').write_text(listing)
    _stub(stubs, 'sudo', textwrap.dedent(f'''\
        echo "$*" >> {tmp_path}/sudo.log
        case "$*" in
          "nft list table inet odin_ci_egress") exec cat {tmp_path}/nft-list.txt ;;
          "nft -f -") exec cat >> {tmp_path}/loaded.nft ;;
          tee*) exec cat >> {tmp_path}/written ;;
        esac'''))
    return subprocess.run(['/bin/bash', str(CI / 'egress-to-prod.sh'), action], env=_base_env(stubs), capture_output=True, text=True)


def _audit_table(packets=0):
    return ('table inet odin_ci_egress {\n'
            f'  chain output {{ ip daddr 192.168.71.211 counter packets {packets} bytes {packets * 60} }}\n'
            '  chain forward { ip daddr 192.168.71.211 counter packets 0 bytes 0 }\n}\n')


def test_egress_audit_counts_on_host_and_container_paths_without_blocking(tmp_path, stubs):
    result = _egress(tmp_path, stubs, 'audit', '')
    assert result.returncode == 0, result.stderr
    loaded = (tmp_path / 'loaded.nft').read_text()
    assert 'hook output' in loaded and 'hook forward' in loaded
    assert loaded.count('ip daddr 192.168.71.211 counter') == 2
    assert 'reject' not in loaded


def test_egress_enforce_needs_a_clean_audit(tmp_path, stubs):
    result = _egress(tmp_path, stubs, 'enforce', _audit_table(packets=3))
    assert result.returncode != 0 and 'reached production' in result.stderr
    assert not (tmp_path / 'loaded.nft').exists()


def test_egress_enforce_rejects_on_both_paths_and_survives_reboot(tmp_path, stubs):
    result = _egress(tmp_path, stubs, 'enforce', _audit_table())
    assert result.returncode == 0, result.stderr
    loaded = (tmp_path / 'loaded.nft').read_text()
    assert loaded.count('ip daddr 192.168.71.211 counter reject') == 2
    log = (tmp_path / 'sudo.log').read_text()
    assert 'systemctl enable odin-ci-egress.service' in log
    assert 'nft -f /etc/odin-ci-egress.nft' in (tmp_path / 'written').read_text()


@pytest.mark.parametrize('listing', ['', 'table inet odin_ci_egress {\n}\n', _audit_table().replace('counter packets 0 bytes 0 }', 'counter packets 0 bytes 0 reject }')])
def test_egress_check_needs_a_real_audit(tmp_path, stubs, listing):
    result = _egress(tmp_path, stubs, 'check', listing)
    assert result.returncode != 0


PRODUCTION = re.compile(r'192\.168\.71\.211|odin\.subsystem\.app')


def _make_recipe(makefile, target):
    block = re.search(rf'^{re.escape(target)}:.*?(?=^\S)', makefile, re.M | re.S)
    assert block, target
    return block.group(0)


def test_CV04_to_CV08_never_name_production():
    """Static half of the check before enforcing; `egress-to-prod.sh audit` proves it on a real run."""
    inventory = json.loads((ROOT / 'ops/release_control/inventory.json').read_text())
    selected = {item['id']: item['command'] for item in inventory['components'] if item['id'] in {'CV04', 'CV05', 'CV06', 'CV07', 'CV08'}}
    assert [command[:2] for _, command in sorted(selected.items())] == [
        ['make', 'test-candidate'], ['make', 'test-database-parity'], ['make', 'test-edu-readiness'],
        ['make', 'test-edu-sandbox'], ['make', 'test-hardware-certification'],
    ]
    roots = ['ops/release_gate', 'ops/database_parity', 'ops/edu_readiness', 'ops/edu_sandbox', 'ops/hardware_certification',
             'ops/demo', 'tests/edu_browser', 'tests/education_browser', 'tests/privacy', 'tests/accessibility',
             'tests/hardware', 'tests/hardware_certification', 'tests/backup_restore', 'docker', 'install']
    naming = set()
    for name in roots:
        for path in (ROOT / name).rglob('*'):
            if path.is_file() and path.suffix in {'.py', '.sh', '.mjs', '.js', '.json', '.yml', '.yaml', '.toml', '.conf'}:
                if PRODUCTION.search(path.read_text(errors='replace')):
                    naming.add(path.relative_to(ROOT).as_posix())
    # verify_live is the operator's read-only live check. No gate runs it.
    assert naming == {'ops/edu_readiness/verify_live.py'}
    makefile = (ROOT / 'Makefile').read_text()
    for target in ('test-candidate', 'test-database-parity', 'test-edu-readiness', 'test-edu-sandbox', 'test-hardware-certification'):
        recipe = _make_recipe(makefile, target)
        assert 'verify_live' not in recipe and 'verify-edu-live' not in recipe


@pytest.mark.parametrize('workflow', ['trusted-validation', 'promote'])
def test_workflows_on_odin_ci_never_name_production(workflow):
    assert not PRODUCTION.search((ROOT / '.github/workflows' / f'{workflow}.yml').read_text())


# ---------------- ad-hoc run guard ----------------

def _guard(stubs, *command, **env):
    return subprocess.run(
        ['/bin/bash', str(CI / 'vm-guarded-run.sh'), *command], capture_output=True, text=True, timeout=60,
        env={**_base_env(stubs), 'VM_GUARD_INTERVAL': '0.1', 'VM_GUARD_GRACE': '1', **env},
    )


@pytest.mark.parametrize('line', [
    LISTENER,
    '/bin/bash -c pgrep -f Runner.Worker',
    'grep Runner.Worker',
    'tail -f /home/odin/actions-runner-runsodin/_diag/Runner.Worker_20261030.log',
    'bash vm-guarded-run.sh echo /x/bin/Runner.Worker',
    '/home/odin/dev/Runner.Worker.notes spawnclient',
])
def test_guard_runs_when_only_a_look_alike_is_listed(tmp_path, stubs, line):
    _ps(stubs, line)
    result = _guard(stubs, '/bin/sh', '-c', 'echo ran')
    assert result.returncode == 0 and result.stdout == 'ran\n', result.stderr


@pytest.mark.parametrize('line', [WORKER, 'Runner.Worker spawnclient 1 2', '/opt/runner/bin/Runner.Worker'])
def test_guard_refuses_during_a_job(tmp_path, stubs, line):
    marker = tmp_path / 'ran'
    _ps(stubs, LISTENER, line)
    result = _guard(stubs, '/usr/bin/touch', str(marker))
    assert result.returncode == 75 and 'refused' in result.stderr
    assert not marker.exists()


def test_guard_refuses_when_the_process_list_cant_be_read(tmp_path, stubs):
    marker = tmp_path / 'ran'
    _stub(stubs, 'ps', 'exit 1')
    assert _guard(stubs, '/usr/bin/touch', str(marker)).returncode == 75
    assert not marker.exists()


def test_guard_sees_a_job_in_a_listing_larger_than_a_pipe(tmp_path, stubs):
    """grep -q stops reading early; with pipefail a piped listing would read as no job."""
    lines = [f'/usr/bin/some-long-running-process --with-an-argument {index:06d}' for index in range(5000)]
    _ps(stubs, WORKER, *lines)
    assert _guard(stubs, '/usr/bin/true').returncode == 75


def test_guard_passes_on_the_command_exit_status(tmp_path, stubs):
    _ps(stubs, LISTENER)
    assert _guard(stubs, '/bin/sh', '-c', 'exit 7').returncode == 7


def test_guard_stops_the_whole_command_when_a_job_starts(tmp_path, stubs):
    _ps(stubs, LISTENER)
    child_pid = tmp_path / 'child.pid'
    # The job appears shortly after the command starts; the command leaves a grandchild behind.
    script = textwrap.dedent(f'''\
        sleep 300 &
        echo $! > {child_pid}
        printf '%s\\n' '{WORKER}' >> {stubs.parent}/ps.txt
        wait
    ''')
    started = time.monotonic()
    result = _guard(stubs, '/bin/sh', '-c', script)
    assert result.returncode == 75 and 'a runner job started' in result.stderr
    assert time.monotonic() - started < 30
    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'}).stdout.strip()
        if not state or state.startswith('Z'):
            break
        time.sleep(0.05)
    else:
        pytest.fail('the command outlived the guard')


@pytest.mark.parametrize('script', ['runner-job-started-hook.sh', 'install-runner-hook.sh', 'egress-to-prod.sh', 'vm-guarded-run.sh'])
def test_host_scripts_are_executable(script):
    assert (CI / script).stat().st_mode & stat.S_IXUSR
