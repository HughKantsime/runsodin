from pathlib import Path

import hashlib
import subprocess
import sys

import pytest
import yaml

from ops.release_control.policy import dispatch_allowed, load_inventory, valid_candidate, workflow_events, workflow_text

ROOT = Path(__file__).parents[2]


def test_WF01_only_manual_dispatch_is_subscribed():
    assert workflow_events(workflow_text()) == {"workflow_dispatch"}


def test_WF02_pull_request_event_is_absent():
    assert "pull_request:" not in workflow_text()


def test_WF03_push_event_is_absent():
    assert "\n  push:" not in workflow_text()


def test_WF04_other_automatic_events_are_absent():
    source = workflow_text()
    for event in ("schedule", "workflow_call", "repository_dispatch", "release", "deployment"):
        assert f"\n  {event}:" not in source


def test_WF05_permissions_are_fail_closed_and_job_read_only():
    source = workflow_text()
    assert "\npermissions: {}\n" in source
    assert "\n    permissions:\n      contents: read\n" in source
    assert "contents: write" not in source


def test_WF06_actor_mismatch_is_rejected():
    assert not dispatch_allowed("intruder", "201174638", "intruder", 1)
    assert 'test "$GITHUB_ACTOR" = "HughKantsime"' in workflow_text()
    assert "\n    if:" not in workflow_text()


def test_WF07_triggering_actor_mismatch_is_rejected():
    assert not dispatch_allowed("HughKantsime", "201174638", "intruder", 1)


def test_WF08_rerun_is_rejected():
    assert not dispatch_allowed("HughKantsime", "201174638", "HughKantsime", 2)
    assert 'test "$GITHUB_RUN_ATTEMPT" = "1"' in workflow_text()


def test_WF09_candidate_ref_shape_is_exact():
    sha = "a" * 40
    assert valid_candidate(sha, f"release-candidate/{sha}")
    assert not valid_candidate(sha, f"refs/heads/release-candidate/{sha}")


def test_WF10_candidate_sha_is_full_lowercase_hex():
    assert not valid_candidate("a" * 39, "release-candidate/" + "a" * 39)
    assert not valid_candidate("G" * 40, "release-candidate/" + "G" * 40)


def test_WF11_runner_labels_are_exact():
    source = workflow_text()
    assert "runs-on: [self-hosted, odin-ci]" in source
    assert "odin-isolated" not in source
    assert "Required tool missing from PATH:" in source


def test_WF12_workflow_has_no_release_side_effect_or_secret_surface():
    source = workflow_text()
    for forbidden in ("secrets.", "environment:", "docker push", "gh release", "git tag", "repository_dispatch"):
        assert forbidden not in source
    assert "make trusted-validation-gate PYTHON=python3.11" in source
    assert "fetch-depth: 0" in source
    candidate_checkout = next(
        step for step in yaml.safe_load(source)["jobs"]["validate"]["steps"]
        if step.get("name") == "Check out immutable candidate"
    )
    assert candidate_checkout["with"] == {
        "ref": "${{ inputs.target_sha }}", "fetch-depth": 0,
        "lfs": True, "persist-credentials": False,
    }
    assert "npm --prefix frontend ci" in source
    assert "python3.11 -m venv .hardware-cert-venv" in source
    assert "python3.11 -m venv .venv-stress" in source
    assert "python3.11 -m venv .venv-contracts" in source
    assert ".venv-contracts/bin/pip install --disable-pip-version-check -r tests/requirements-contracts.txt" in source
    inventory = load_inventory()
    for component in inventory["components"]:
        assert component["working_directory"] == "."
        assert component["environment_allowlist"]
        assert component["expected_artifacts"]
        assert "timeout_seconds" in component
        assert component["source"]


@pytest.mark.parametrize("workflow,job", [
    ("trusted-validation.yml", "validate"), ("publish-image.yml", "publish"),
])
@pytest.mark.parametrize("state", ["hydrated", "pointer", "wrong-size", "wrong-hash", "missing"])
def test_pinned_vision_model_verification_rejects_missing_or_corrupt_materialization(tmp_path, workflow, job, state):
    payload = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    steps = payload["jobs"][job]["steps"]
    checkout_index = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/checkout@"))
    verify = steps[checkout_index + 1]
    assert verify["name"] == "Materialize and verify pinned vision model"
    assert verify.get("working-directory", ".") == ("candidate" if job == "publish" else ".")
    assert "git lfs checkout -- backend/vision_models_default/obico_spaghetti.onnx" in verify["run"]
    script = verify["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    content = b"fixture model bytes"
    model = tmp_path / "backend/vision_models_default/obico_spaghetti.onnx"
    model.parent.mkdir(parents=True)
    pointer = ("version https://git-lfs.github.com/spec/v1\n"
               f"oid sha256:{hashlib.sha256(content).hexdigest()}\nsize {len(content)}\n")
    model.write_text(pointer)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.test", "commit", "-qm", "model pointer"],
                   check=True, capture_output=True)
    if state == "hydrated":
        model.write_bytes(content)
    elif state == "wrong-size":
        model.write_bytes(content + b"x")
    elif state == "wrong-hash":
        model.write_bytes(b"x" * len(content))
    elif state == "missing":
        model.unlink()
    result = subprocess.run([sys.executable, "-P", "-c", script], cwd=tmp_path,
                            capture_output=True, text=True)
    assert (result.returncode == 0) == (state == "hydrated"), result.stderr


@pytest.mark.parametrize('name,tools', [
    ('trusted-validation', ('python3.11', 'node', 'npm', 'mosquitto', 'docker', 'git', 'git-lfs', 'make', 'gitleaks', 'semgrep', 'hadolint')),
    ('promote', ('python3.11', 'gh')),
    ('publish-image', ('python3.11', 'gh', 'docker', 'git', 'git-lfs')),
    ('promote-production', ('python3.11', 'gh', 'docker')),
    ('rollback-2.0-production', ('docker',)),
])
def test_all_release_workflows_pin_linux_runner_and_fail_closed_path_tools(name, tools):
    source = (ROOT / '.github/workflows' / f'{name}.yml').read_text()
    workflow = yaml.safe_load(source)
    assert workflow['permissions'] == {}
    assert len(workflow['jobs']) == 1
    job = next(iter(workflow['jobs'].values()))
    assert job['runs-on'] == ['self-hosted', 'odin-ci']
    bootstrap = job['steps'][0]['run']
    for tool in tools:
        assert f'command -v {tool} >/dev/null || {{ echo "Required tool missing from PATH: {tool}" >&2; exit 1; }}' in bootstrap
    assert 'HughKantsime' in bootstrap and '201174638' in bootstrap
    assert 'TRIGGERING_ACTOR' in bootstrap and 'RUN_ATTEMPT' in bootstrap
    for forbidden in ('/Users/ollama', '/opt/homebrew', 'mac-mini-runner', 'Colima', 'Rosetta', 'setup-qemu'):
        assert forbidden not in source
    for step in job['steps']:
        if 'uses' in step:
            assert len(step['uses'].rsplit('@', 1)[-1]) == 40
    if 'python3.11' in tools:
        assert 'sys.version_info[:2] == (3, 11)' in bootstrap
    if 'docker' in tools:
        assert 'Docker Engine 29.x is required' in bootstrap
        assert 'Docker buildx plugin is required' in bootstrap


def test_multi_arch_publication_requires_native_daemon_and_host_arm64_emulation():
    source = (ROOT / '.github/workflows/publish-image.yml').read_text()
    steps = yaml.safe_load(source)['jobs']['publish']['steps']
    by_name = {step['name']: step for step in steps}
    assert 'docker buildx build --push --platform linux/amd64,linux/arm64 --provenance=false' in source
    native = by_name['Require native Linux amd64 Docker']['run']
    assert '$(uname -s)' in native and '$(uname -m)' in native
    assert 'linux/x86_64' in native
    emulation = by_name['Require arm64 emulation']['run']
    assert 'set -euo pipefail' in emulation
    assert 'docker run --rm --platform linux/arm64' in emulation
    assert 'python:3.11-slim@sha256:0b23cfb7425d065008b778022a17b1551c82f8b4866ee5a7a200084b7e2eafbf' in emulation
    assert 'platform.machine()' in emulation
    assert 'test "$machine" = aarch64' in emulation
    names = [step['name'] for step in steps]
    assert names.index('Require native Linux amd64 Docker') < names.index('Require arm64 emulation') < names.index('Set up Docker Buildx')
    for forbidden in ('setup-qemu', 'apt-get', '--privileged', 'tonistiigi/binfmt'):
        assert forbidden not in source
    manifest = by_name['Exercise exact amd64 and arm64 manifests']['run']
    assert 'publication_platforms' in manifest
    assert 'expected = {("linux", "amd64"), ("linux", "arm64")}' in manifest
    assert 'assert {(row["os"], row["architecture"]) for row in rows} == expected' in manifest
    assert 'for arch in amd64 arm64; do' in manifest
    assert 'test "$healthy" = 1' in manifest
    assert 'test "$observed" = "$INPUT_VERSION"' in manifest
    production = (ROOT / '.github/workflows/promote-production.yml').read_text()
    assert 'description: Published multi-platform OCI index digest' in production
    helper = (ROOT / 'ops/release_control/mutation_workflow.py').read_text()
    assert '--prefer-index=false' not in helper


def test_linux_bootstrap_uses_owned_python_and_browser_dependencies():
    source = workflow_text()
    assert '"$GITHUB_WORKSPACE/.venv-contracts/bin" >> "$GITHUB_PATH"' in source
    assert '.venv-contracts/bin/python -m playwright install chromium' in source
    assert 'npx --no-install playwright install chromium' in source
    assert 'pip-audit==' not in source and 'bandit==' not in source
    requirements = (ROOT / 'tests/requirements-contracts.txt').read_text().splitlines()
    assert 'pip-audit==2.10.0' in requirements
    assert 'bandit==1.9.4' in requirements
    assert '-r ../backend/requirements.txt' in requirements
    assert 'test "$(node --version)" = v25.9.0' in source


@pytest.mark.parametrize('name', ['promote', 'publish-image', 'promote-production', 'rollback-2.0-production'])
def test_mutation_workflows_never_install_runtime_dependencies(name):
    source = (ROOT / '.github/workflows' / f'{name}.yml').read_text()
    for forbidden in ('pip install', '/pip" install', '-m pip', '-m venv', 'uv pip', 'apt-get', 'setup-python'):
        assert forbidden not in source
    assert '/runtime/bin' not in source


@pytest.mark.parametrize('name', ['promote', 'publish-image', 'promote-production'])
@pytest.mark.parametrize('state', ['exact', 'wrong-version', 'missing-module', 'missing-metadata'])
def test_host_jsonschema_requirement_fails_closed(name, state, monkeypatch):
    import importlib.metadata
    from types import ModuleType

    source = (ROOT / '.github/workflows' / f'{name}.yml').read_text()
    job = next(iter(yaml.safe_load(source)['jobs'].values()))
    bootstrap = job['steps'][0]['run']
    script = bootstrap.split("python3.11 -P <<'PYTHON'\n", 1)[1].split('\nPYTHON', 1)[0]
    monkeypatch.setitem(sys.modules, 'jsonschema', None if state == 'missing-module' else ModuleType('jsonschema'))

    def version(package):
        assert package == 'jsonschema'
        if state == 'missing-metadata':
            raise importlib.metadata.PackageNotFoundError(package)
        return '4.24.0' if state == 'wrong-version' else '4.25.1'

    monkeypatch.setattr(importlib.metadata, 'version', version)
    if state == 'exact':
        exec(compile(script, name, 'exec'), {})
    else:
        with pytest.raises(SystemExit, match=r'Host python3.11.*jsonschema==4.25.1.*provision the runner'):
            exec(compile(script, name, 'exec'), {})
