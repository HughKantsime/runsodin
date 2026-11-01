"""The runner host scripts under ops/ci, always behind stub tools: nothing here touches a real
runner, service or firewall, and the only processes killed are ones these tests start."""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
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
    # The scripts branch on this. A test wanting the Mac's branch stubs Darwin; the host never decides.
    _stub(directory, 'uname', 'echo Linux')
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


# A failed hook alone doesn't stop later `if: always()` steps, so every refusal has to end the worker.

LINUX_WORKER = 'Runner.Worker'
MAC_WORKER = '/Users/ollama/actions-runner-runsodin/bin.2.338.0/Runner.Worker'  # as ps reports it on the Mac


def _hook_values(tmp_path, stubs, **env):
    payload = tmp_path / 'event.json'
    if not payload.exists():
        payload.write_text(json.dumps(_event()))
    jq = shutil.which('jq')
    assert jq, 'jq is required by the hook'
    return {
        'PATH': f'{stubs}:{Path(jq).parent}:/usr/bin:/bin', 'HOME': str(tmp_path),
        'GITHUB_RUN_ID': '777', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_REPOSITORY': 'HughKantsime/runsodin', 'GITHUB_REPOSITORY_ID': '1143865575',
        'GITHUB_ACTOR': 'HughKantsime', 'GITHUB_ACTOR_ID': '201174638',
        'GITHUB_TRIGGERING_ACTOR': 'HughKantsime', 'GITHUB_EVENT_PATH': str(payload), **env,
    }


def _under_parent(tmp_path, stubs, parent_name, **env):
    """Run the hook as the runner does, `bash -e <hook>`, under a throwaway parent that ps reports
    as parent_name. The parent records surviving, so a kill shows as both -9 and no record."""
    _stub(stubs, 'ps', f'echo "{parent_name}"')
    survived = tmp_path / 'parent-survived'
    drop = env.pop('drop', ())
    values = _hook_values(tmp_path, stubs, **env)
    for name in drop:
        del values[name]
    result = subprocess.run(['/bin/bash', '-c', f'/bin/bash -e {HOOK}; echo "$?" > {survived}'],
                            env=values, capture_output=True, text=True, timeout=30)
    return result, survived


@pytest.mark.parametrize('worker', [LINUX_WORKER, MAC_WORKER])
@pytest.mark.parametrize('env', [
    {'GITHUB_EVENT_NAME': 'pull_request'},
    {'GITHUB_EVENT_NAME': 'push'},
    {'GITHUB_ACTOR': 'returning-contributor'},
    {'GITHUB_REPOSITORY': 'someone-else/runsodin'},
    {'drop': ('GITHUB_EVENT_NAME',)},
    {'drop': ('GITHUB_ACTOR_ID',)},
    {'drop': ('GITHUB_EVENT_PATH',)},
    {'GITHUB_EVENT_PATH': '/nonexistent/event.json'},
], ids=lambda env: str(env)[:60])
def test_every_refusal_kills_the_worker_so_always_steps_cant_run(tmp_path, stubs, worker, env):
    result, survived = _under_parent(tmp_path, stubs, worker, **env)
    assert result.returncode == -9, result.stderr
    assert not survived.exists()
    assert 'killing Runner.Worker' in result.stderr


def test_closed_stderr_cant_stop_the_kill(tmp_path, stubs):
    """Under set -e, a failing echo before the kill would end the script with the job still running."""
    _stub(stubs, 'ps', f'echo "{LINUX_WORKER}"')
    survived = tmp_path / 'parent-survived'
    result = subprocess.run(['/bin/bash', '-c', f'/bin/bash -e {HOOK} 2>&-; echo "$?" > {survived}'],
                            env=_hook_values(tmp_path, stubs, GITHUB_EVENT_NAME='push'),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == -9, result.stderr
    assert not survived.exists()


def test_broken_stderr_cant_stop_the_kill(tmp_path, stubs):
    """A pipe nobody reads raises SIGPIPE on the first echo, which would end the script outright."""
    _stub(stubs, 'ps', f'echo "{LINUX_WORKER}"')
    survived = tmp_path / 'parent-survived'
    reader, writer = os.pipe()
    os.close(reader)
    try:
        result = subprocess.run(['/bin/bash', '-c', f'/bin/bash -e {HOOK}; echo "$?" > {survived}'],
                                env=_hook_values(tmp_path, stubs, GITHUB_EVENT_NAME='push'),
                                stdout=subprocess.DEVNULL, stderr=writer, timeout=30)
    finally:
        os.close(writer)
    assert result.returncode == -9
    assert not survived.exists()
    assert 'killing Runner.Worker' in (tmp_path / '.odin-runner-job-hook.log').read_text()


def test_fifo_at_the_log_path_cant_stop_the_kill(tmp_path, stubs):
    """Opening a FIFO nobody reads blocks for good, and the kill comes after the record."""
    log = tmp_path / '.odin-runner-job-hook.log'
    os.mkfifo(log)
    result, survived = _under_parent(tmp_path, stubs, LINUX_WORKER, GITHUB_EVENT_NAME='push')
    assert result.returncode == -9, result.stderr
    assert not survived.exists()
    assert stat.S_ISFIFO(log.stat().st_mode)


def test_directory_at_the_log_path_cant_stop_the_kill(tmp_path, stubs):
    (tmp_path / '.odin-runner-job-hook.log').mkdir()
    result, survived = _under_parent(tmp_path, stubs, LINUX_WORKER, GITHUB_EVENT_NAME='push')
    assert result.returncode == -9, result.stderr
    assert not survived.exists()


def test_an_existing_log_file_is_appended_to(tmp_path, stubs):
    log = tmp_path / '.odin-runner-job-hook.log'
    log.write_text('an earlier refusal\n')
    result, _ = _under_parent(tmp_path, stubs, LINUX_WORKER, GITHUB_EVENT_NAME='push')
    assert result.returncode == -9, result.stderr
    lines = log.read_text().splitlines()
    assert lines[0] == 'an earlier refusal' and 'event=push' in lines[1]


def test_missing_jq_kills_the_worker(tmp_path, stubs):
    # Only the stubs on PATH, so the check itself can't run. The stub ps needs nothing else.
    result, survived = _under_parent(tmp_path, stubs, LINUX_WORKER, PATH=str(stubs))
    assert result.returncode == -9, result.stderr
    assert not survived.exists()


def test_disagreeing_payload_kills_the_worker(tmp_path, stubs):
    (tmp_path / 'event.json').write_text(json.dumps(_event(sender={'login': 'returning-contributor', 'id': 1})))
    result, survived = _under_parent(tmp_path, stubs, LINUX_WORKER)
    assert result.returncode == -9, result.stderr
    assert not survived.exists()


def test_a_crash_inside_the_hook_kills_the_worker_too(tmp_path, stubs):
    """Anything short of the pass at the end counts, even a failure nobody wrote a refusal for."""
    broken = tmp_path / 'hook.sh'
    broken.write_text(HOOK.read_text().replace('[ -f "${GITHUB_EVENT_PATH:-}" ]', 'false; [ -f "${GITHUB_EVENT_PATH:-}" ]'))
    _stub(stubs, 'ps', f'echo "{LINUX_WORKER}"')
    survived = tmp_path / 'parent-survived'
    result = subprocess.run(['/bin/bash', '-c', f'/bin/bash -e {broken}; echo "$?" > {survived}'],
                            env=_hook_values(tmp_path, stubs), capture_output=True, text=True, timeout=30)
    assert result.returncode == -9, result.stderr
    assert not survived.exists()


@pytest.mark.parametrize('worker', [LINUX_WORKER, MAC_WORKER])
def test_owner_dispatch_leaves_the_worker_running(tmp_path, stubs, worker):
    result, survived = _under_parent(tmp_path, stubs, worker)
    assert result.returncode == 0, result.stderr
    assert survived.read_text().strip() == '0'


@pytest.mark.parametrize('name', ['bash', 'python3.11', 'Runner.Worker.notes', 'NotRunner.Worker', 'Runner.Listener', '/x/Runner.Worker/bash', ''])
def test_refusal_only_kills_a_process_named_runner_worker(tmp_path, stubs, name):
    result, survived = _under_parent(tmp_path, stubs, name, GITHUB_EVENT_NAME='push')
    assert result.returncode == 0, result.stderr  # the parent lived to record the hook's exit
    assert survived.read_text().strip() == '1'
    assert 'is not Runner.Worker' in result.stderr


def test_refusal_keeps_the_parent_when_ps_fails(tmp_path, stubs):
    _stub(stubs, 'ps', 'exit 1')
    survived = tmp_path / 'parent-survived'
    values = _hook_values(tmp_path, stubs, GITHUB_EVENT_NAME='push')
    result = subprocess.run(['/bin/bash', '-c', f'/bin/bash -e {HOOK}; echo "$?" > {survived}'],
                            env=values, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and survived.read_text().strip() == '1'
    assert 'unknown is not Runner.Worker' in result.stderr


def test_refusal_never_reaches_past_the_direct_parent(tmp_path, stubs):
    """The test suite runs under a real worker, so this hook must never walk up to it."""
    _stub(stubs, 'ps', textwrap.dedent('''\
        if [ "$4" = "$FAKE_WORKER" ]; then echo Runner.Worker; else echo bash; fi'''))
    survived = tmp_path / 'worker-survived'
    values = _hook_values(tmp_path, stubs, GITHUB_EVENT_NAME='push')
    script = f'''export FAKE_WORKER=$$; /bin/bash -c '/bin/bash -e {HOOK}; true'; echo alive > {survived}'''
    result = subprocess.run(['/bin/bash', '-c', script], env=values, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert survived.read_text().strip() == 'alive'


def test_refusal_records_why_on_the_host(tmp_path, stubs):
    """The kill stops the runner uploading the step's log, so this file is what proves a refusal."""
    result, _ = _under_parent(tmp_path, stubs, MAC_WORKER, GITHUB_EVENT_NAME='push')
    assert result.returncode == -9
    line = (tmp_path / '.odin-runner-job-hook.log').read_text().splitlines()[-1]
    assert 'run=777 event=push actor=HughKantsime repository=HughKantsime/runsodin' in line
    assert 'why=[event push is not workflow_dispatch]' in line
    assert re.search(r'action=\[killing Runner\.Worker [0-9]+\]$', line)


def test_refusal_is_recorded_before_the_kill(tmp_path, stubs):
    """The hook's own kill is wrapped to note whether the record already exists at that moment."""
    _stub(stubs, 'ps', f'echo "{LINUX_WORKER}"')
    log = tmp_path / '.odin-runner-job-hook.log'
    order = tmp_path / 'order'
    script = textwrap.dedent(f'''\
        kill() {{ if [ -s {log} ]; then echo recorded-first > {order}; fi; builtin kill "$@"; }}
        export -f kill
        /bin/bash -e {HOOK}
        echo survived
    ''')
    result = subprocess.run(['/bin/bash', '-c', script], env=_hook_values(tmp_path, stubs, GITHUB_EVENT_NAME='push'),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == -9, result.stderr
    assert order.read_text().strip() == 'recorded-first'


@pytest.mark.parametrize('home', ['/nonexistent/home', None])
def test_a_log_that_cant_be_written_does_not_save_the_job(tmp_path, stubs, home):
    values = _hook_values(tmp_path, stubs, GITHUB_EVENT_NAME='push')
    if home:
        values['HOME'] = home
    else:
        del values['HOME']
    _stub(stubs, 'ps', f'echo "{LINUX_WORKER}"')
    survived = tmp_path / 'parent-survived'
    result = subprocess.run(['/bin/bash', '-c', f'/bin/bash -e {HOOK}; echo "$?" > {survived}'],
                            env=values, capture_output=True, text=True, timeout=30)
    assert result.returncode == -9, result.stderr
    assert not survived.exists()


def test_refusal_records_a_parent_it_left_running(tmp_path, stubs):
    result, _ = _under_parent(tmp_path, stubs, 'bash', GITHUB_EVENT_NAME='push')
    assert result.returncode == 0
    assert 'action=[parent bash kept running]' in (tmp_path / '.odin-runner-job-hook.log').read_text()


def test_a_crash_is_recorded_too(tmp_path, stubs):
    broken = tmp_path / 'hook.sh'
    broken.write_text(HOOK.read_text().replace('[ -f "${GITHUB_EVENT_PATH:-}" ]', 'false; [ -f "${GITHUB_EVENT_PATH:-}" ]'))
    _stub(stubs, 'ps', f'echo "{LINUX_WORKER}"')
    subprocess.run(['/bin/bash', '-c', f'/bin/bash -e {broken}; true'], env=_hook_values(tmp_path, stubs), capture_output=True, text=True, timeout=30)
    assert 'why=[the hook stopped before its checks finished]' in (tmp_path / '.odin-runner-job-hook.log').read_text()


def test_owner_dispatch_records_nothing(tmp_path, stubs):
    result, _ = _under_parent(tmp_path, stubs, LINUX_WORKER)
    assert result.returncode == 0
    assert not (tmp_path / '.odin-runner-job-hook.log').exists()


def test_readme_proof_checks_what_survives_the_kill():
    """The kill comes before the step's log uploads, so the proof can't ask for hook lines in the run log."""
    readme = (CI / 'README.md').read_text()
    proof = readme[readme.index('### Prove it'):readme.index('### Keep it from drifting')]
    for needed in ('The run fails.', '~/.odin-runner-job-hook.log', 'killing Runner.Worker', 'HOOK PROOF FAILED', 'Runner.Listener'):
        assert needed in proof
    assert 'log has "runner job hook refuses this job"' not in proof


def test_readme_lists_the_commands_that_clean_the_running_vm():
    readme = (CI / 'README.md').read_text()
    for command in ('python3.11 -m pip uninstall -y --break-system-packages jsonschema jsonschema-specifications referencing rpds-py',
                    'python3.11 -m pip uninstall -y --break-system-packages pip', 'sudo apt-get purge -y gh'):
        assert command in readme


def test_readme_cleanup_check_allows_for_ubuntus_own_jsonschema():
    """python3-jsonschema is a cloud-init dependency, so the import still resolves after the cleanup."""
    readme = (CI / 'README.md').read_text()
    section = readme[readme.index('### Remove what eligibility left'):readme.index('## Egress to production')]
    assert "`python3.11 -c 'import jsonschema'` fails" not in section
    assert "print(jsonschema.__file__)" in section and '~/.local' in section
    assert 'python3-jsonschema' in section and '/usr/lib/python3/dist-packages' in section


if sys.platform == 'linux':  # macOS kills copied system interpreters, and a skip would fail the gate there
    def test_refusal_kills_a_real_process_named_runner_worker(tmp_path, stubs):
        worker = tmp_path / 'Runner.Worker'
        shutil.copy2(Path(sys.executable).resolve(), worker)
        survived = tmp_path / 'parent-survived'
        values = _hook_values(tmp_path, stubs, GITHUB_EVENT_NAME='push')
        driver = (f"import subprocess; subprocess.run(['/bin/bash', '-e', {str(HOOK)!r}]); "
                  f"open({str(survived)!r}, 'w').write('alive')")
        result = subprocess.run([str(worker), '-c', driver], env=values, capture_output=True, text=True, timeout=30)
        assert result.returncode == -9, result.stderr
        assert not survived.exists()


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


def test_a_host_that_says_darwin_does_not_reach_the_scripts(tmp_path, stubs):
    """What these tests see on a real Mac, whose uname sits behind the stubs on PATH."""
    host = tmp_path / 'host-bin'
    host.mkdir()
    _stub(host, 'uname', 'echo Darwin')
    (tmp_path / '.service').write_text('actions.runner.synthetic.service\n')
    _ps(stubs, LISTENER)
    result = _lib('runner_restart', tmp_path, stubs, PATH=f'{stubs}:{host}:/usr/bin:/bin')
    assert result.returncode == 0, result.stderr
    assert (stubs.parent / 'sudo.log').read_text() == 'systemctl restart actions.runner.synthetic.service\n'


def test_restart_on_darwin_uses_the_runner_service_script_without_sudo(tmp_path, stubs):
    _stub(stubs, 'uname', 'echo Darwin')
    (tmp_path / '.service').write_text('/Users/ollama/Library/LaunchAgents/actions.runner.synthetic.plist\n')
    (tmp_path / 'svc.sh').write_text(f'#!/bin/sh\necho "$*" >> {tmp_path}/svc.log\n')
    (tmp_path / 'svc.sh').chmod(0o755)
    _ps(stubs, LISTENER)
    result = _lib('runner_restart', tmp_path, stubs)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'svc.log').read_text().splitlines() == ['stop', 'start']
    assert not (stubs.parent / 'sudo.log').exists()


def test_restart_refuses_during_a_job(tmp_path, stubs):
    (tmp_path / '.service').write_text('actions.runner.synthetic.service\n')
    _ps(stubs, LISTENER, WORKER)
    result = _lib('runner_restart', tmp_path, stubs)
    assert result.returncode == 1 and 'after the job: actions.runner.synthetic.service' in result.stderr
    assert not (stubs.parent / 'sudo.log').exists()


def test_restart_before_registration_touches_nothing(tmp_path, stubs):
    _ps(stubs, LISTENER)
    assert _lib('runner_restart', tmp_path, stubs).returncode == 0
    assert not (stubs.parent / 'sudo.log').exists()


def test_tools_provisioning_replaces_an_old_tmpdir_and_restarts_after_a_change():
    source = (CI / 'provision-odin-ci-tools.sh').read_text()
    assert 'if runner_env_set TMPDIR "$ci_tmp"; then\n  runner_restart\nfi' in source
    assert "grep -qs '^TMPDIR='" not in source


def test_provisioning_drops_what_only_eligibility_needed():
    """Eligibility runs on the Mac now, so provisioning adds no gh and no user-site jsonschema.
    Ubuntu's own python3-jsonschema stays, as a cloud-init dependency nothing here imports."""
    for script in ('provision-odin-ci.sh', 'provision-odin-ci-tools.sh'):
        source = (CI / script).read_text()
        assert not re.search(r'\bjsonschema\b|\bcli/cli\b|\bgh_\$|\bgh --version|--break-system-packages|ensurepip', source)
    workflow = (ROOT / '.github/workflows/trusted-validation.yml').read_text()
    assert 'command -v gh' not in workflow and 'gh api' not in workflow
    # The gate's own jsonschema comes from the contracts venv, which the workflow puts first on PATH.
    assert '"$GITHUB_WORKSPACE/.venv-contracts/bin" >> "$GITHUB_PATH"' in workflow
    assert 'jsonschema==' in (ROOT / 'backend/requirements.txt').read_text()
    assert '-r ../backend/requirements.txt' in (ROOT / 'tests/requirements-contracts.txt').read_text()


def test_qemu_stays_for_the_registry_contract_not_for_publishing():
    source = (CI / 'provision-odin-ci-tools.sh').read_text()
    assert 'qemu-user-static' in source
    header = source.split('set -euo pipefail', 1)[0]
    assert 'registry contract' in header and 'arm64 image half' not in header
    contract = (ROOT / 'tests/test_release_control/test_registry_integration.py').read_text()
    assert '"linux/amd64,linux/arm64"' in contract and 'RUN ' in contract


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
        'install -d -m 0755 -o 0 -g 0 /usr/local/libexec/odin-ci',
        f'install -m 0755 -o 0 -g 0 {HOOK} {hook}',
        'systemctl restart actions.runner.synthetic.service',
    ]


def test_installer_on_the_mac_restarts_the_launch_agent_without_sudo(tmp_path, stubs):
    runner = tmp_path / 'runner'
    runner.mkdir()
    (runner / '.env').write_text('LANG=en_US.UTF-8\n')
    (runner / '.service').write_text('/Users/ollama/Library/LaunchAgents/actions.runner.synthetic.plist\n')
    (runner / 'svc.sh').write_text(f'#!/bin/sh\necho "svc.sh $*" >> {tmp_path}/svc.log\n')
    (runner / 'svc.sh').chmod(0o755)
    _stub(stubs, 'uname', 'echo Darwin')
    _ps(stubs, '/Users/ollama/actions-runner-runsodin/bin/Runner.Listener')
    _, result = _install(tmp_path, stubs)
    assert result.returncode == 0, result.stderr
    hook = '/usr/local/libexec/odin-ci/runner-job-started-hook.sh'
    assert (runner / '.env').read_text() == f'LANG=en_US.UTF-8\nACTIONS_RUNNER_HOOK_JOB_STARTED={hook}\n'
    assert (tmp_path / 'svc.log').read_text().splitlines() == ['svc.sh stop', 'svc.sh start']
    # macOS has no group called root, and a launch agent must not be restarted as root.
    log = (tmp_path / 'sudo.log').read_text()
    assert '-g root' not in log and '-o root' not in log and 'systemctl' not in log and 'svc.sh' not in log


def test_installer_refuses_during_a_job_on_the_mac(tmp_path, stubs):
    _stub(stubs, 'uname', 'echo Darwin')
    _ps(stubs, MAC_WORKER + ' spawnclient 157 160')
    runner, result = _install(tmp_path, stubs)
    assert result.returncode == 75
    assert not (tmp_path / 'sudo.log').exists() and not (runner / '.env').exists()


@pytest.mark.parametrize('script', ['runner-job-started-hook.sh', 'runner-lib.sh', 'install-runner-hook.sh'])
def test_scripts_the_mac_runs_stay_within_bash_32(script):
    """No associative arrays, ${var^^}, mapfile or |&: all newer than the Mac's /bin/bash."""
    source = (CI / script).read_text()
    for forbidden in ('declare -A', 'local -A', 'mapfile', 'readarray', '|&', '^^}', ',,}', '${var@', 'coproc', 'wait -n'):
        assert forbidden not in source


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


def test_workflow_on_odin_ci_never_names_production():
    assert not PRODUCTION.search((ROOT / '.github/workflows/trusted-validation.yml').read_text())


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


def test_the_way_back_works_from_a_clean_clone(tmp_path):
    """A clean clone has no local revert branch, only the remote's. The README's own commands, on a stand-in remote."""
    def git(*arguments, cwd):
        return subprocess.run(['git', *arguments], cwd=cwd, capture_output=True, text=True,
                              env={**os.environ, 'GIT_AUTHOR_NAME': 'ci', 'GIT_AUTHOR_EMAIL': 'ci@invalid',
                                   'GIT_COMMITTER_NAME': 'ci', 'GIT_COMMITTER_EMAIL': 'ci@invalid'})
    remote = tmp_path / 'remote'
    remote.mkdir()
    assert git('init', '-q', '-b', 'main', cwd=remote).returncode == 0
    (remote / 'workflow').write_text('runner\n')
    git('add', '.', cwd=remote)
    git('commit', '-qm', 'main', cwd=remote)
    git('switch', '-qc', 'revert/validation-to-mac', cwd=remote)
    (remote / 'workflow').write_text('mac\n')
    git('commit', '-qam', 'back to the mac', cwd=remote)
    git('switch', '-q', 'main', cwd=remote)
    assert git('clone', '-q', str(remote), 'clone', cwd=tmp_path).returncode == 0
    clone = tmp_path / 'clone'
    readme = (CI / 'README.md').read_text()
    section = readme[readme.index('## Moving validation back to the Mac'):readme.index('## Egress to production')]
    commands = [line.strip() for line in section.splitlines() if line.strip().startswith(('git fetch origin', 'git switch -c revert-validation', 'git cherry-pick'))]
    assert len(commands) == 3
    for command in commands:
        ran = git(*command.split()[1:], cwd=clone)
        assert ran.returncode == 0, f'{command}: {ran.stderr}'
    assert (clone / 'workflow').read_text() == 'mac\n'
    assert git('rev-parse', '--verify', '-q', 'revert/validation-to-mac', cwd=clone).returncode != 0, 'no local branch: the prefix is needed'


def test_readme_gives_the_way_back_to_the_mac():
    readme = (CI / 'README.md').read_text()
    section = readme[readme.index('## Moving validation back to the Mac'):readme.index('## Egress to production')]
    steps = [
        '.odin-release-chain.lock',
        'git cherry-pick origin/revert/validation-to-mac',
        'pytest tests/test_release_control',
        'git push origin HEAD:main',
        'gh variable delete ODIN_RUNNER_HOOK_REQUIRED',
        'release-candidate/<SHA>',
    ]
    positions = [section.index(step) for step in steps]
    assert positions == sorted(positions)


@pytest.mark.parametrize('script', ['runner-job-started-hook.sh', 'install-runner-hook.sh', 'egress-to-prod.sh', 'vm-guarded-run.sh'])
def test_host_scripts_are_executable(script):
    assert (CI / script).stat().st_mode & stat.S_IXUSR
