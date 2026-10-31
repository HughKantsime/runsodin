"""Portability contracts without starting browsers, brokers, mounts, or Docker."""
import ast
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ops.release_control import aggregate

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('platform,xdg,override,expected', [
    ('linux', None, None, '/home/odin/.cache/ms-playwright'),
    ('linux', '/cache', None, '/cache/ms-playwright'),
    ('darwin', None, None, '/home/odin/Library/Caches/ms-playwright'),
    ('linux', None, '/browsers', '/browsers'),
    ('darwin', None, '/browsers', '/browsers'),
])
def test_browser_cache_is_resolved_before_home_isolation(monkeypatch, platform, xdg, override, expected):
    monkeypatch.setattr(aggregate.sys, 'platform', platform)
    monkeypatch.setattr(Path, 'home', lambda: Path('/home/odin'))
    for key, value in [('XDG_CACHE_HOME', xdg), ('PLAYWRIGHT_BROWSERS_PATH', override)]:
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    assert aggregate.playwright_browsers_path() == expected


@pytest.mark.parametrize('free,passes', [(4 * 1024**3, True), (1024**3, False), (1000000 * 1024**3, False)])
def test_linux_browser_upload_fixture_uses_real_filesystem_without_hdiutil(tmp_path, free, passes):
    # Load only the fixture definition: importing its module would load Playwright.
    source = ROOT / 'tests/education_browser/test_pilot_journey.py'
    node = next(node for node in ast.parse(source.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == 'upload_volume')
    node.decorator_list = []
    namespace = {'sys': SimpleNamespace(platform='linux'), 'shutil': SimpleNamespace(disk_usage=lambda _: SimpleNamespace(free=free))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    fixture = namespace['upload_volume'](tmp_path)
    if passes:
        assert next(fixture) == tmp_path / 'upload-volume'
        with pytest.raises(StopIteration):
            next(fixture)
    else:
        with pytest.raises(AssertionError, match='CV06 needs'):
            next(fixture)


MAC = ['self-hosted', 'mac-mini-runner', 'm4']
ODIN_CI = ['self-hosted', 'odin-ci']


@pytest.mark.parametrize('workflow,labels', [
    ('trusted-validation', ODIN_CI),
    ('promote', MAC),
    ('publish-image', MAC),
    ('promote-production', MAC),
    ('rollback-2.0-production', MAC),
])
def test_each_release_workflow_runs_on_its_named_host(workflow, labels):
    """Only validation moved: arm64 under QEMU never went healthy on the VM, and eligibility
    authenticates validation's evidence, so it stays off the host that ran candidate code."""
    parsed = yaml.safe_load((ROOT / '.github/workflows' / f'{workflow}.yml').read_text())
    assert [job['runs-on'] for job in parsed['jobs'].values()] == [labels]


def test_actionlint_knows_every_host_label():
    labels = yaml.safe_load((ROOT / '.github/actionlint.yaml').read_text())['self-hosted-runner']['labels']
    assert set(labels) == {'mac-mini-runner', 'm4', 'odin-ci'}


@pytest.mark.parametrize('workflow,artifact', [
    ('promote', '/actions/artifacts/${artifact_id}/zip'),
    ('publish-image', '/actions/artifacts/${validation_artifact_id}/zip'),
    ('promote-production', '/actions/artifacts/${validation_artifact_id}/zip'),
])
def test_evidence_crosses_hosts_only_as_an_uploaded_artifact(workflow, artifact):
    """A job on the Mac can't see odin-ci's disk, so nothing may read validation's local directory."""
    source = (ROOT / '.github/workflows' / f'{workflow}.yml').read_text()
    assert artifact in source
    assert 'verify-download' in source
    assert 'artifacts/trusted-validation' not in source
    assert '_work/' not in source and 'actions-runner' not in source


def test_validation_uploads_the_evidence_other_hosts_download():
    source = (ROOT / '.github/workflows/trusted-validation.yml').read_text()
    assert 'name: odin-trusted-validation-${{ github.run_id }}-${{ github.run_attempt }}' in source
    assert 'path: artifacts/trusted-validation/${{ env.RELEASE_CONTROL_RUN_ID }}/evidence/' in source
    assert 'if-no-files-found: error' in source


def test_gates_keep_the_runner_tracking_id_so_the_runner_can_find_strays(monkeypatch):
    """The runner kills a finished job's leftovers by this variable; a server started under a
    gate that dropped it would survive into the next job."""
    from ops.release_control.policy import load_inventory
    from ops.release_control.run_gate import BASE_ENVIRONMENT_ALLOWLIST, command_environment

    monkeypatch.setenv('RUNNER_TRACKING_ID', 'github_synthetic')
    assert command_environment()['RUNNER_TRACKING_ID'] == 'github_synthetic'
    assert 'RUNNER_TRACKING_ID' in BASE_ENVIRONMENT_ALLOWLIST
    assert load_inventory()  # refuses an inventory whose allowlist differs from the code's


@pytest.mark.parametrize('system,stops', [('Linux', True), ('Darwin', False), ('FreeBSD', True)])
def test_rollback_stops_on_the_wrong_host(tmp_path, system, stops):
    """Its bootstrap finds docker wherever PATH says, so without this it would run on any host."""
    workflow = yaml.safe_load((ROOT / '.github/workflows/rollback-2.0-production.yml').read_text())
    bootstrap = workflow['jobs']['rollback']['steps'][0]['run']
    line = next(item for item in bootstrap.splitlines() if 'uname -s' in item)
    assert bootstrap.index(line) < bootstrap.index('docker_bin=')
    (tmp_path / 'uname').write_text(f'#!/bin/sh\necho {system}\n')
    (tmp_path / 'uname').chmod(0o755)
    result = subprocess.run(['/bin/bash', '-c', f'set -euo pipefail\n{line}\necho went-on'],
                            env={'PATH': f'{tmp_path}:/usr/bin:/bin'}, capture_output=True, text=True)
    assert (result.returncode != 0) is stops
    assert ('went-on' in result.stdout) is not stops


def _hook_drift_check():
    workflow = yaml.safe_load((ROOT / '.github/workflows/trusted-validation.yml').read_text())
    step = workflow['jobs']['validate']['steps'][0]
    script = step['run']
    start = script.index('case "${RUNNER_HOOK_REQUIRED:-}" in')
    return step, script[start:script.index('\nfi\n', start) + len('\nfi\n')]


# Where the installer puts the hook. Tests point the check at a stand-in, since only root can
# create this path; test_validation_checks_the_path_the_installer_uses keeps the two together.
HOOK_PATH = '/usr/local/libexec/odin-ci/runner-job-started-hook.sh'


def _run_drift_check(tmp_path, hook=None, required=None, path_prefix='', installed_at=HOOK_PATH):
    env = {'PATH': f'{path_prefix}/usr/bin:/bin'}
    if hook is not None:
        env['ACTIONS_RUNNER_HOOK_JOB_STARTED'] = str(hook)
    if required is not None:
        env['RUNNER_HOOK_REQUIRED'] = required
    check = _hook_drift_check()[1].replace(HOOK_PATH, str(installed_at))
    return subprocess.run(['/bin/bash', '-c', 'set -euo pipefail\n' + check + 'echo went-on\n'],
                          env=env, capture_output=True, text=True, timeout=30)


# A file root really owns, in a directory root really owns, on Linux and macOS alike.
ROOT_OWNED = '/usr/bin/env'


def test_validation_runs_the_drift_check_before_anything_else_it_needs():
    step, check = _hook_drift_check()
    assert step['env']['RUNNER_HOOK_REQUIRED'] == '${{ vars.ODIN_RUNNER_HOOK_REQUIRED }}'
    assert step['run'].index(check) < step['run'].index('command -v python3.11')
    assert '-user root ! -perm -020 ! -perm -002' in check


def test_validation_checks_the_path_the_installer_uses():
    assert f'test "$hook" = {HOOK_PATH} ||' in _hook_drift_check()[1]
    assert f'hook={HOOK_PATH}\n' in (ROOT / 'ops/ci/install-runner-hook.sh').read_text()


@pytest.mark.parametrize('required', [None, ''])
def test_before_the_hook_is_installed_validation_warns_and_goes_on(tmp_path, required):
    result = _run_drift_check(tmp_path, required=required)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('::warning title=Runner job hook not installed::')
    assert 'went-on' in result.stdout


def test_a_hook_required_but_lost_fails_validation(tmp_path):
    result = _run_drift_check(tmp_path, required='true')
    assert result.returncode != 0 and 'went-on' not in result.stdout
    assert 'ODIN_RUNNER_HOOK_REQUIRED is true' in result.stderr


@pytest.mark.parametrize('required', ['TRUE', 'True', '1', 'yes', 'false', ' true', 'true '])
@pytest.mark.parametrize('hook', [None, ROOT_OWNED])
def test_any_other_value_of_the_variable_fails_rather_than_warns(tmp_path, required, hook):
    """A mistyped value with the hook lost would otherwise only warn, looking enforced when it isn't."""
    result = _run_drift_check(tmp_path, hook=hook, required=required, installed_at=ROOT_OWNED)
    assert result.returncode != 0 and 'went-on' not in result.stdout
    assert 'must be true or empty' in result.stderr


@pytest.mark.parametrize('required', [None, '', 'true'])
def test_the_installed_root_owned_hook_passes_quietly(tmp_path, required):
    result = _run_drift_check(tmp_path, hook=ROOT_OWNED, required=required, installed_at=ROOT_OWNED)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'went-on\n'


@pytest.mark.parametrize('required', [None, 'true'])
def test_any_other_root_owned_file_is_not_the_hook(tmp_path, required):
    result = _run_drift_check(tmp_path, hook=ROOT_OWNED, required=required)
    assert result.returncode != 0 and 'went-on' not in result.stdout
    assert f'must be {HOOK_PATH}' in result.stderr


@pytest.mark.parametrize('required', [None, 'true'])
@pytest.mark.parametrize('kind', ['user-owned', 'missing', 'symlink', 'directory'])
def test_a_set_hook_is_checked_even_before_the_variable_is_set(tmp_path, kind, required):
    """An install mistake shows on the first run after it, whatever the repository variable says."""
    hook = tmp_path / 'hook.sh'
    if kind == 'user-owned':
        hook.write_text('#!/bin/sh\n')
    elif kind == 'symlink':
        hook.symlink_to(ROOT_OWNED)
    elif kind == 'directory':
        hook.mkdir()
    result = _run_drift_check(tmp_path, hook=hook, required=required, installed_at=hook)
    assert result.returncode != 0 and 'went-on' not in result.stdout


def test_a_root_owned_directory_is_not_a_hook(tmp_path):
    result = _run_drift_check(tmp_path, hook='/usr/bin', installed_at='/usr/bin')
    assert result.returncode != 0 and 'not a regular file' in result.stderr


def test_a_symlink_fails_even_if_it_and_its_directory_look_root_owned(tmp_path):
    """Where a symlink's own mode passes the find below, as on macOS, this check is all that catches it."""
    hook = tmp_path / 'hook.sh'
    hook.symlink_to(ROOT_OWNED)
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    (stubs / 'find').write_text('#!/bin/sh\necho one\necho two\n')
    (stubs / 'find').chmod(0o755)
    result = _run_drift_check(tmp_path, hook=hook, path_prefix=f'{stubs}:', installed_at=hook)
    assert result.returncode != 0 and 'not a regular file' in result.stderr


def test_a_root_owned_hook_in_a_directory_others_can_write_fails(tmp_path):
    """Whoever owns the directory can swap the file, so the directory has to pass too."""
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    # Answers for the file and not for its directory, which can't be set up here without root.
    (stubs / 'find').write_text(f'#!/bin/sh\necho "{ROOT_OWNED}"\n')
    (stubs / 'find').chmod(0o755)
    result = _run_drift_check(tmp_path, hook=ROOT_OWNED, path_prefix=f'{stubs}:', installed_at=ROOT_OWNED)
    assert result.returncode != 0 and 'went-on' not in result.stdout
    assert 'owned and writable by root alone' in result.stderr


def _hook_copy_step():
    steps = yaml.safe_load((ROOT / '.github/workflows/trusted-validation.yml').read_text())['jobs']['validate']['steps']
    names = [step['name'] for step in steps]
    return steps, names, names.index('Check the installed job hook matches this candidate')


def _run_copy_check(tmp_path, installed=None, candidate='hook v1\n'):
    """Run the workflow's step in a stand-in checkout holding the candidate's hook, or none."""
    checkout = tmp_path / 'checkout'
    (checkout / 'ops/ci').mkdir(parents=True)
    if candidate is not None:
        (checkout / 'ops/ci/runner-job-started-hook.sh').write_text(candidate)
    env = {'PATH': '/usr/bin:/bin'}
    if installed is not None:
        path = tmp_path / 'installed-hook.sh'
        path.write_text(installed)
        env['ACTIONS_RUNNER_HOOK_JOB_STARTED'] = str(path)
    steps, _, index = _hook_copy_step()
    return subprocess.run(['/bin/bash', '-c', steps[index]['run'] + 'echo went-on\n'], cwd=checkout, env=env,
                          capture_output=True, text=True, timeout=30)


def test_the_hook_comparison_runs_after_checkout_and_before_any_candidate_code():
    steps, names, index = _hook_copy_step()
    assert names.index('Check out immutable candidate') < index < names.index('Bootstrap clean-checkout validation dependencies')
    assert 'cmp "$hook" ops/ci/runner-job-started-hook.sh' in steps[index]['run']
    assert 'if' not in steps[index]


def test_an_installed_hook_identical_to_the_candidates_passes(tmp_path):
    result = _run_copy_check(tmp_path, installed='hook v1\n')
    assert result.returncode == 0, result.stderr
    assert 'went-on' in result.stdout


def test_a_successful_comparison_says_what_it_compared(tmp_path):
    """cmp prints nothing when files match, so without this line a run can't show the compare happened."""
    result = _run_copy_check(tmp_path, installed='hook v1\n')
    assert result.returncode == 0, result.stderr
    installed = tmp_path / 'installed-hook.sh'
    assert result.stdout == f'Installed job hook {installed} matches ops/ci/runner-job-started-hook.sh\nwent-on\n'


def test_no_match_is_claimed_without_a_comparison_that_matched(tmp_path):
    differs = _run_copy_check(tmp_path, installed='hook v1\n', candidate='hook v2\n')
    assert 'matches' not in differs.stdout
    assert 'matches' not in _run_copy_check(tmp_path / 'no-hook').stdout


def test_a_candidate_that_changed_the_hook_fails_until_it_is_installed(tmp_path):
    result = _run_copy_check(tmp_path, installed='hook v1\n', candidate='hook v2\n')
    assert result.returncode != 0 and 'went-on' not in result.stdout
    assert "Install the candidate's copy first" in result.stderr


def test_a_candidate_without_the_hook_fails_once_one_is_installed(tmp_path):
    result = _run_copy_check(tmp_path, installed='hook v1\n', candidate=None)
    assert result.returncode != 0 and 'went-on' not in result.stdout


def test_the_comparison_does_nothing_before_the_hook_is_installed(tmp_path):
    """The bootstrap already warned, or failed if the hook is required."""
    result = _run_copy_check(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'went-on' in result.stdout


def test_readme_gives_the_order_for_changing_the_hook():
    readme = (ROOT / 'ops/ci/README.md').read_text()
    section = readme[readme.index('### Changing the hook'):readme.index('### Remove what eligibility left')]
    assert section.index('install-runner-hook.sh') < section.index('Dispatch validation')
