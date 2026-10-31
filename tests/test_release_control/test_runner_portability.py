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
