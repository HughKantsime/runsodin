from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from ops.release_control import promotion_workflow as workflow
from ops.release_control.practical_evidence import build_bundle, verify_bundle
from ops.release_control.promotion_eligibility import EligibilityError, verify_eligibility

SHA = "a" * 40
WORKFLOW_SHA = "e" * 40
VALIDATION_SHA = "d" * 40
DIGEST = "b" * 64
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[2]


def _inputs(action: str = "stage") -> dict[str, object]:
    return {
        "action": action,
        "validation_run_id": 100,
        "candidate_sha": SHA,
        "candidate_ref": f"release-candidate/{SHA}",
        "evidence_sha256": DIGEST,
        "nonce": "nonce_1234567890abcdef",
        "authorization_text": f"Authorize exact ODIN {action} eligibility",
    }


def _current(action: str = "stage") -> dict[str, object]:
    return {
        "repository_id": 77,
        "repository": "HughKantsime/odin",
        "run_id": 200,
        "run_attempt": 1,
        "actor_login": "HughKantsime",
        "actor_id": 201174638,
        "triggering_actor": "HughKantsime",
        "event": "workflow_dispatch",
        "workflow_path": ".github/workflows/promote.yml",
        "workflow_ref": "HughKantsime/odin/.github/workflows/promote.yml@refs/heads/main",
        "workflow_sha": WORKFLOW_SHA,
        "action": action,
    }


def _run() -> dict[str, object]:
    return {
        "id": 100,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "conclusion": "success",
        "path": ".github/workflows/trusted-validation.yml",
        "head_sha": VALIDATION_SHA,
        "head_branch": "main",
        "repository": {"id": 77, "full_name": "HughKantsime/odin"},
        "actor": {"login": "HughKantsime", "id": 201174638},
        "triggering_actor": {"login": "HughKantsime", "id": 201174638},
    }


def _artifact(name: str = "odin-trusted-validation-100-1") -> dict[str, object]:
    return {
        "id": 900,
        "name": name,
        "digest": "sha256:" + "c" * 64,
        "expired": False,
        "workflow_run": {
            "id": 100,
            "repository_id": 77,
            "head_repository_id": 77,
            "head_sha": VALIDATION_SHA,
            "head_branch": "main",
        },
    }


def _approval() -> list[dict[str, object]]:
    return [{
        "state": "approved",
        "user": {"login": "HughKantsime", "id": 201174638},
        "environments": [{"name": "production"}],
    }]


def _artifacts(*items: dict[str, object], total_count: int | None = None) -> dict[str, object]:
    return {"total_count": len(items) if total_count is None else total_count, "artifacts": list(items)}


def _expect(code: str, function, *args, **kwargs) -> None:
    with pytest.raises((workflow.PromotionWorkflowError, EligibilityError)) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def _observation() -> dict[str, object]:
    return workflow.select_observation(_run(), _artifacts(_artifact()), 100)[0]


def _evidence_nodes(path: Path) -> None:
    path.mkdir()
    for name in workflow.EVIDENCE_FILES:
        (path / name).write_text("safe\n", encoding="utf-8")


def _evidence_zip(path: Path, names: tuple[str, ...] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names or tuple(workflow.EVIDENCE_FILES):
            archive.writestr(name, "safe\n")
    return path


def _contents_responses() -> list[dict[str, object]]:
    responses = []
    for relative in workflow.FETCH_ALLOWLIST:
        data = (ROOT / relative).read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        responses.append({
            "path": relative,
            "type": "file",
            "encoding": "base64",
            "size": len(data),
            "sha": blob,
            "content": "\n".join(
                base64.b64encode(data).decode("ascii")[index:index + 60]
                for index in range(0, len(base64.b64encode(data).decode("ascii")), 60)
            ),
        })
    return responses


def test_PI01_exact_valid_stage_inputs_context_pass():
    workflow.validate_inputs(_inputs(), _current())


def test_PI02_wrong_actor_login_or_id_rejects():
    for key, value in (("actor_login", "intruder"), ("actor_id", 1)):
        current = _current(); current[key] = value
        _expect("ACTOR_MISMATCH", workflow.validate_inputs, _inputs(), current)


def test_PI03_triggering_actor_mismatch_rejects():
    current = _current(); current["triggering_actor"] = "intruder"
    _expect("TRIGGERING_ACTOR_MISMATCH", workflow.validate_inputs, _inputs(), current)


def test_PI04_attempt_two_rejects():
    current = _current(); current["run_attempt"] = 2
    _expect("RUN_ATTEMPT_REJECTED", workflow.validate_inputs, _inputs(), current)


def test_PI05_invalid_action_rejects():
    inputs = _inputs(); inputs["action"] = "deploy"
    _expect("AUTHORIZATION_SCOPE_MISMATCH", workflow.validate_inputs, inputs, _current())


def test_PI06_invalid_validation_run_id_rejects():
    for value in (0, -1, "100", True):
        inputs = _inputs(); inputs["validation_run_id"] = value
        _expect("OBSERVATION_INVALID", workflow.validate_inputs, inputs, _current())


def test_PI07_candidate_sha_ref_mismatch_rejects():
    for key, value in (("candidate_sha", "A" * 40), ("candidate_ref", "release-candidate/" + "b" * 40)):
        inputs = _inputs(); inputs[key] = value
        _expect("CANDIDATE_MISMATCH", workflow.validate_inputs, inputs, _current())


def test_PI08_invalid_evidence_digest_rejects():
    inputs = _inputs(); inputs["evidence_sha256"] = "x" * 64
    _expect("EVIDENCE_DIGEST_MISMATCH", workflow.validate_inputs, inputs, _current())


def test_PI09_invalid_nonce_rejects():
    inputs = _inputs(); inputs["nonce"] = "too-short"
    _expect("AUTHORIZATION_INVALID", workflow.validate_inputs, inputs, _current())


def test_PI10_empty_or_oversized_text_rejects():
    for value in ("", "x" * 1001):
        inputs = _inputs(); inputs["authorization_text"] = value
        _expect("AUTHORIZATION_INVALID", workflow.validate_inputs, inputs, _current())


def test_PO01_official_matching_payload_normalizes():
    observed, artifact_id = workflow.select_observation(_run(), _artifacts(_artifact()), 100)
    assert artifact_id == 900 and observed["path"] == ".github/workflows/trusted-validation.yml"


def test_PO02_zero_or_multiple_artifacts_reject():
    _expect("ARTIFACT_MISMATCH", workflow.select_observation, _run(), _artifacts(), 100)
    _expect("ARTIFACT_MISMATCH", workflow.select_observation, _run(), _artifacts(_artifact(), _artifact()), 100)
    _expect("ARTIFACT_MISMATCH", workflow.select_observation, _run(), _artifacts(_artifact(), total_count=2), 100)


def test_PO03_artifact_run_id_mismatch_rejects():
    artifact = _artifact(); artifact["workflow_run"]["id"] = 101
    _expect("ARTIFACT_MISMATCH", workflow.select_observation, _run(), _artifacts(artifact), 100)


def test_PO04_artifact_repository_or_sha_mismatch_rejects():
    for key, value in (("repository_id", 78), ("head_repository_id", 78), ("head_sha", "f" * 40)):
        artifact = _artifact(); artifact["workflow_run"][key] = value
        _expect("ARTIFACT_MISMATCH", workflow.select_observation, _run(), _artifacts(artifact), 100)


def test_PO05_validation_attempt_two_rejects():
    run = _run(); run["run_attempt"] = 2
    _expect("RUN_ATTEMPT_REJECTED", workflow.select_observation, run, _artifacts(_artifact()), 100)


def test_PO06_non_manual_event_rejects():
    run = _run(); run["event"] = "push"
    _expect("EVENT_REJECTED", workflow.select_observation, run, _artifacts(_artifact()), 100)


def test_PO07_non_success_conclusion_rejects():
    run = _run(); run["conclusion"] = "failure"
    _expect("VALIDATION_NOT_SUCCESSFUL", workflow.select_observation, run, _artifacts(_artifact()), 100)


def test_PO08_validation_identity_mismatch_rejects():
    run = _run(); run["actor"]["id"] = 1
    _expect("ACTOR_MISMATCH", workflow.select_observation, run, _artifacts(_artifact()), 100)
    run = _run(); run["triggering_actor"]["login"] = "intruder"
    _expect("TRIGGERING_ACTOR_MISMATCH", workflow.select_observation, run, _artifacts(_artifact()), 100)


def test_PO09_workflow_path_or_branch_rejects():
    for key, value in (("path", "wrong.yml"), ("head_branch", "release-candidate")):
        run = _run(); run[key] = value
        _expect("WORKFLOW_IDENTITY_MISMATCH", workflow.select_observation, run, _artifacts(_artifact()), 100)


def test_PO10_artifact_name_mismatch_rejects():
    _expect("ARTIFACT_MISMATCH", workflow.select_observation, _run(), _artifacts(_artifact("wrong")), 100)
    artifact = _artifact(); artifact["expired"] = True
    _expect("ARTIFACT_MISMATCH", workflow.select_observation, _run(), _artifacts(artifact), 100)


def test_PD01_exact_three_file_download_and_digest_pass(tmp_path, monkeypatch):
    source = tmp_path / "source"; _evidence_nodes(source)
    artifact_zip = tmp_path / "evidence.zip"
    with zipfile.ZipFile(artifact_zip, "w") as archive:
        for name in workflow.EVIDENCE_FILES:
            archive.write(source / name, name)
    monkeypatch.setattr(workflow, "verify_bundle", lambda _: {"evidence_sha256": DIGEST, "manifest": {}})
    assert workflow.extract_and_verify(artifact_zip, tmp_path / "evidence", DIGEST)["evidence_sha256"] == DIGEST


def test_PD02_symlink_or_special_node_rejects_without_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "verify_bundle", lambda _: {"evidence_sha256": DIGEST})
    for kind in ("symlink", "fifo"):
        artifact_zip = tmp_path / f"{kind}.zip"
        with zipfile.ZipFile(artifact_zip, "w") as archive:
            archive.writestr("manifest.json", "safe\n")
            archive.writestr("manifest.sha256", "safe\n")
            special = zipfile.ZipInfo("index.html")
            special.create_system = 3
            node_type = stat.S_IFLNK if kind == "symlink" else stat.S_IFIFO
            special.external_attr = (node_type | 0o600) << 16
            archive.writestr(special, "manifest.json")
        evidence = tmp_path / kind
        _expect("EVIDENCE_NODE_FORBIDDEN", workflow.extract_and_verify, artifact_zip, evidence, DIGEST)
        assert not evidence.exists()


def test_PD03_missing_extra_or_nested_file_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "verify_bundle", lambda _: {"evidence_sha256": DIGEST})
    variants = {
        "missing": ("manifest.json", "manifest.sha256"),
        "extra": ("manifest.json", "manifest.sha256", "index.html", "extra.txt"),
        "nested": ("manifest.json", "manifest.sha256", "nested/index.html"),
    }
    for kind, names in variants.items():
        artifact_zip = _evidence_zip(tmp_path / f"{kind}.zip", names)
        evidence = tmp_path / kind
        _expect("EVIDENCE_INVALID", workflow.extract_and_verify, artifact_zip, evidence, DIGEST)
        assert not evidence.exists()


def test_PD04_oversized_file_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "MAX_FILE_BYTES", 16)
    artifact_zip = tmp_path / "oversized.zip"
    with zipfile.ZipFile(artifact_zip, "w") as archive:
        archive.writestr("manifest.json", "safe")
        archive.writestr("manifest.sha256", "safe")
        archive.writestr("index.html", "x" * 17)
    monkeypatch.setattr(workflow, "verify_bundle", lambda _: {"evidence_sha256": DIGEST})
    _expect("EVIDENCE_FILE_TOO_LARGE", workflow.extract_and_verify, artifact_zip, tmp_path / "evidence", DIGEST)


def test_PD05_manifest_or_authorized_digest_mismatch_rejects(tmp_path, monkeypatch):
    artifact_zip = _evidence_zip(tmp_path / "mismatch.zip")
    monkeypatch.setattr(workflow, "verify_bundle", lambda _: {"evidence_sha256": "f" * 64})
    evidence = tmp_path / "evidence"
    _expect("EVIDENCE_DIGEST_MISMATCH", workflow.extract_and_verify, artifact_zip, evidence, DIGEST)
    assert not evidence.exists()


def test_PD06_render_escapes_canary_and_writes_atomic_outputs(tmp_path):
    request = {"action": "<script>alert(1)</script>", "candidate_sha": SHA, "evidence_sha256": DIGEST}
    workflow.render_decision(tmp_path, {"status": "eligible"}, request, {"run_id": 1}, {"run_id": 2})
    page = (tmp_path / "index.html").read_text()
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert {item.name for item in tmp_path.iterdir()} == {
        "request.json", "validation-observation.json", "promotion-context.json", "decision.json", "index.html"
    }


def test_PF01_exact_contents_allowlist_publishes_and_imports(tmp_path, monkeypatch):
    output = tmp_path / "bundle"
    workflow.fetch_files(_contents_responses(), output)
    assert {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()} == set(workflow.FETCH_ALLOWLIST)
    check = subprocess.run(
        [sys.executable, "-P", "-c", (
            "import json,ops.release_control.promotion_workflow as p,ops.edu_readiness.artifact_scan as a;"
            "print(json.dumps([p.__file__,a.__file__]))"
        )],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(output), "PYTHONSAFEPATH": "1"},
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = json.loads(check.stdout)
    assert all(Path(path).is_relative_to(output) for path in loaded)


def test_PF02_bad_contents_or_preexisting_output_never_publishes(tmp_path):
    mutations = []
    base = _contents_responses()
    mutations.append(base[:-1])
    extra = _contents_responses(); extra.append(dict(extra[0])); mutations.append(extra)
    duplicate = _contents_responses(); duplicate[-1] = dict(duplicate[0]); mutations.append(duplicate)
    wrong_path = _contents_responses(); wrong_path[0]["path"] = "wrong.py"; mutations.append(wrong_path)
    wrong_size = _contents_responses(); wrong_size[0]["size"] += 1; mutations.append(wrong_size)
    wrong_blob = _contents_responses(); wrong_blob[0]["sha"] = "0" * 40; mutations.append(wrong_blob)
    for index, responses in enumerate(mutations):
        output = tmp_path / f"bad-{index}"
        with pytest.raises(workflow.PromotionWorkflowError):
            workflow.fetch_files(responses, output)
        assert not output.exists()
    output = tmp_path / "exists"; output.mkdir()
    _expect("FETCH_OUTPUT_EXISTS", workflow.fetch_files, _contents_responses(), output)
    linked = tmp_path / "linked"; linked.symlink_to(output, target_is_directory=True)
    _expect("FETCH_OUTPUT_EXISTS", workflow.fetch_files, _contents_responses(), linked)


def test_PR01_generated_stage_and_production_records_pass_real_verifier(tmp_path, monkeypatch):
    practical_path = ROOT / "tests/test_release_control/test_practical_evidence.py"
    spec = importlib.util.spec_from_file_location("practical_fixture", practical_path)
    assert spec and spec.loader
    fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
    source = fixture._run(tmp_path, monkeypatch)
    bundle = build_bundle(source, SHA)
    evidence_digest = verify_bundle(bundle)["evidence_sha256"]
    validation = _observation()
    for action, approval in (("stage", []), ("production", _approval())):
        inputs = _inputs(action); inputs["evidence_sha256"] = evidence_digest
        request, context = workflow.build_records(inputs, _current(action), validation, approval, now=NOW)
        request_path = tmp_path / f"{action}-request.json"
        validation_path = tmp_path / f"{action}-validation.json"
        context_path = tmp_path / f"{action}-context.json"
        for path, payload in ((request_path, request), (validation_path, validation), (context_path, context)):
            path.write_text(json.dumps(payload), encoding="utf-8")
        assert verify_eligibility(bundle, request_path, validation_path, context_path, now=NOW)["status"] == "eligible"
    request["candidate_ref"] = "release-candidate/" + "f" * 40
    request_path.write_text(json.dumps(request), encoding="utf-8")
    _expect("CANDIDATE_MISMATCH", verify_eligibility, bundle, request_path, validation_path, context_path, now=NOW)


def _workflow_text() -> str:
    return (ROOT / ".github/workflows/promote.yml").read_text(encoding="utf-8")


def test_PW01_manual_only_actual_runner_and_dynamic_environment():
    source = _workflow_text()
    assert "pull_request:" not in source and "push:" not in source
    assert "workflow_dispatch:" in source
    assert "runs-on: [self-hosted, mac-mini-runner, m4]" in source
    assert "environment: ${{ inputs.action }}" in source
    assert 'PROMOTION_READY: "0"' in source
    assert "PROMOTION_OUTPUT: ${{ github.workspace }}/.odin-promotion-uninitialized-${{ github.run_id }}-${{ github.run_attempt }}/output" in source
    assert "if: always() && env.PROMOTION_READY == '1'" in source
    assert 'echo "PROMOTION_READY=1"' in source
    assert "test -x /Users/ollama/homebrew/bin/python3.11" in source
    assert "test -x /opt/homebrew/bin/gh" in source
    assert "printf '%s\\n' /Users/ollama/homebrew/bin /opt/homebrew/bin >> \"$GITHUB_PATH\"" in source


def test_PW02_permissions_pins_and_no_checkout():
    source = _workflow_text(); payload = yaml.safe_load(source)
    job = payload["jobs"]["eligibility"]
    assert payload["permissions"] == {} and job["permissions"] == {"actions": "read", "contents": "read"}
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    assert uses == ["actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"]
    assert "actions/checkout" not in source and "persist-credentials" not in source


def test_PW03_inputs_are_environment_data_not_shell_expressions():
    payload = yaml.safe_load(_workflow_text())
    for step in payload["jobs"]["eligibility"]["steps"]:
        if "run" in step:
            assert "${{ inputs." not in step["run"]
    helper_steps = payload["jobs"]["eligibility"]["steps"][1:5]
    assert all('cd "$PROMOTION_ROOT"' in step["run"] for step in helper_steps)


def test_PW04_no_release_deploy_or_write_surface():
    source = _workflow_text().lower()
    forbidden = ("docker build", "docker push", "kubectl", "helm ", "npm publish", "gh release", "git tag", "git push", "secrets: write", "packages: write", "contents: write")
    assert all(item not in source for item in forbidden)
