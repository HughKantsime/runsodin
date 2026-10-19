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
    assert "runs-on: [self-hosted, mac-mini-runner, m4]" in source
    assert "odin-isolated" not in source
    assert "printf '%s\\n' /Users/ollama/homebrew/bin /Users/ollama/homebrew/sbin /opt/homebrew/bin >> \"$GITHUB_PATH\"" in source


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
