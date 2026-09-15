from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from ops.release_control import practical_evidence as evidence_module
from ops.release_control.practical_evidence import EvidenceError, build_bundle, verify_bundle
from ops.release_control.promotion_eligibility import EligibilityError, verify_eligibility
from ops.release_control.release_authorization import text_sha256

SHA = "a" * 40
OTHER_SHA = "b" * 40
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _result(gate_id: str, tests: int = 1) -> dict[str, object]:
    return {
        "schema_version": 1, "gate_id": gate_id, "status": "pass", "command": ["true"],
        "working_directory": "/ephemeral/checkout", "started_at": "2026-09-15T11:00:00Z",
        "ended_at": "2026-09-15T11:01:00Z", "duration_seconds": 60, "exit_code": 0,
        "timed_out": False,
        "counts": {"tests": tests, "passed": tests, "failures": 0, "errors": 0, "skipped": 0, "xfailed": 0},
        "tool_versions": {"python": "3.11"},
        "log": {"path": "/ephemeral/result.log", "sha256": "0" * 64, "sanitized": True},
        "artifacts": [], "findings": [],
    }


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    trusted = tmp_path / "trusted-validation"
    source = trusted / "gha-100-1"
    source.mkdir(parents=True)
    monkeypatch.setattr(evidence_module, "TRUSTED_ROOT", trusted)
    manifests = {
        "CV04": (source / "native/candidate/one/manifest.json", {"commit": SHA, "dirty": False}),
        "CV05": (source / "native/database-parity/one/manifest.json", {"commit": SHA, "dirty": False}),
        "CV07": (source / "native/edu-sandbox/one/manifest.json", {"source_commit": SHA, "source_dirty": False}),
        "CV08": (source / "native/hardware/one/manifest.json", {"git_commit": SHA[:7], "git_dirty": False}),
    }
    for path, payload in manifests.values():
        _write(path, payload)
    artifacts = []
    for number in range(1, 11):
        component_id = f"CV{number:02d}"
        path = source / "components" / component_id / "result.json"
        result = _result(component_id.lower())
        if component_id in manifests:
            native_path = manifests[component_id][0]
        else:
            native_path = source / "native/support" / f"{component_id}.json"
            _write(native_path, {"component": component_id, "status": "pass"})
        result["artifacts"] = [{"path": str(native_path), "sha256": hashlib.sha256(native_path.read_bytes()).hexdigest()}]
        _write(path, result)
        data = path.read_bytes()
        artifacts.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest()})
    aggregate = _result("trusted_validation", 10)
    aggregate["artifacts"] = artifacts
    _write(source / "result.json", aggregate)
    _write(source / "index.html", {"safe": True})
    _write(source / "junit.xml", {"safe": True})
    return source


def _refresh_component(source: Path, component_id: str) -> None:
    component_path = source / "components" / component_id / "result.json"
    component = json.loads(component_path.read_text())
    for artifact in component["artifacts"]:
        artifact["sha256"] = hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
    _write(component_path, component)
    aggregate_path = source / "result.json"
    aggregate = json.loads(aggregate_path.read_text())
    for artifact in aggregate["artifacts"]:
        if Path(artifact["path"]) == component_path:
            artifact["sha256"] = hashlib.sha256(component_path.read_bytes()).hexdigest()
    _write(aggregate_path, aggregate)


def _expect(code: str, function, *args) -> None:
    with pytest.raises((EvidenceError, EligibilityError)) as caught:
        function(*args)
    assert caught.value.code == code


def _promotion_files(source: Path, tmp_path: Path, action: str = "stage") -> tuple[Path, Path, Path]:
    verified = verify_bundle(source / "evidence")
    evidence_sha = verified["evidence_sha256"]
    environment = action
    text = f"Authorize ODIN {action} for {SHA}"
    request = {
        "schema_version": 1, "action": action, "repository_id": 77, "repository": "HughKantsime/odin",
        "candidate_sha": SHA, "candidate_ref": f"release-candidate/{SHA}", "evidence_sha256": evidence_sha,
        "validation_run_id": 100, "validation_run_attempt": 1, "validation_workflow_sha": "d" * 40,
        "validation_artifact_name": "odin-trusted-validation-100-1", "validation_artifact_digest": "sha256:" + "c" * 64,
        "promotion_run_id": 200, "promotion_run_attempt": 1, "promotion_workflow_sha": "e" * 40,
        "actor_login": "HughKantsime",
        "actor_id": 201174638, "triggering_actor": "HughKantsime", "environment": environment,
        "nonce": "nonce_1234567890abcdef", "issued_at": "2026-09-15T11:45:00Z",
        "expires_at": "2026-09-15T12:45:00Z" if action == "production" else "2026-09-16T11:45:00Z",
        "authorization_text": text, "authorization_text_sha256": text_sha256(text),
    }
    validation = {
        "workflow_run": {
            "id": 100, "run_attempt": 1, "event": "workflow_dispatch", "conclusion": "success",
            "path": ".github/workflows/trusted-validation.yml", "head_sha": "d" * 40,
            "head_branch": "main", "repository": {"id": 77, "full_name": "HughKantsime/odin"},
            "actor": {"login": "HughKantsime", "id": 201174638},
            "triggering_actor": {"login": "HughKantsime", "id": 201174638},
        },
        "artifact": {
            "name": "odin-trusted-validation-100-1", "digest": "sha256:" + "c" * 64,
            "workflow_run": {"id": 100, "repository_id": 77, "head_sha": "d" * 40},
        },
    }
    review = None
    if action == "production":
        review = {"state": "approved", "actor_login": "HughKantsime", "actor_id": 201174638, "run_id": 200}
    promotion = {
        "schema_version": 1, "repository_id": 77, "repository": "HughKantsime/odin", "run_id": 200,
        "run_attempt": 1, "actor_login": "HughKantsime", "actor_id": 201174638,
        "triggering_actor": "HughKantsime", "event": "workflow_dispatch",
        "workflow_path": ".github/workflows/promote.yml",
        "workflow_ref": "HughKantsime/odin/.github/workflows/promote.yml@refs/heads/main",
        "workflow_sha": "e" * 40, "action": action, "environment": environment,
        "evidence_sha256": evidence_sha, "nonce": request["nonce"],
        "authorization_text_sha256": request["authorization_text_sha256"], "environment_review": review,
    }
    paths = (tmp_path / "request.json", tmp_path / "validation.json", tmp_path / "promotion.json")
    for path, payload in zip(paths, (request, validation, promotion)):
        _write(path, payload)
    return paths


def _eligible(source: Path, paths: tuple[Path, Path, Path]) -> dict[str, object]:
    return verify_eligibility(source / "evidence", *paths, now=NOW)


def test_PE01_complete_pass_is_eligible(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch)
    bundle = build_bundle(source, SHA)
    manifest = verify_bundle(bundle)["manifest"]
    assert manifest["status"] == "eligible" and len(manifest["components"]) == 10
    assert manifest["source_commit"] == SHA


def test_PE02_build_is_byte_deterministic(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch)
    first_bytes = (build_bundle(source, SHA) / "manifest.json").read_bytes()
    shutil.rmtree(source / "evidence")
    second_bytes = (build_bundle(source, SHA) / "manifest.json").read_bytes()
    assert first_bytes == second_bytes


def test_PE03_failed_aggregate_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); payload = json.loads((source / "result.json").read_text())
    payload["status"] = "fail"; _write(source / "result.json", payload)
    _expect("AGGREGATE_NOT_PASSING", build_bundle, source, SHA)


def test_PE04_missing_component_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); payload = json.loads((source / "result.json").read_text())
    payload["artifacts"] = payload["artifacts"][:-1]; _write(source / "result.json", payload)
    _expect("COMPONENT_SET_MISMATCH", build_bundle, source, SHA)


def test_PE05_extra_component_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); extra = source / "components/CV11/result.json"
    native = source / "native/support/CV11.json"; _write(native, {"component": "CV11", "status": "pass"})
    result = _result("cv11"); result["artifacts"] = [{"path": str(native), "sha256": hashlib.sha256(native.read_bytes()).hexdigest()}]
    _write(extra, result)
    payload = json.loads((source / "result.json").read_text())
    payload["artifacts"].append({"path": str(extra), "sha256": hashlib.sha256(extra.read_bytes()).hexdigest()})
    _write(source / "result.json", payload); _expect("COMPONENT_SET_MISMATCH", build_bundle, source, SHA)


def test_PE06_component_tamper_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path / "component", monkeypatch); (source / "components/CV04/result.json").write_text("{}\n")
    _expect("COMPONENT_HASH_MISMATCH", build_bundle, source, SHA)
    source = _run(tmp_path / "native", monkeypatch)
    _write(source / "native/candidate/one/manifest.json", {"commit": OTHER_SHA, "dirty": False})
    _expect("NATIVE_ARTIFACT_HASH_MISMATCH", build_bundle, source, SHA)


def test_PE07_source_disagreement_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); _write(source / "native/hardware/one/manifest.json", {"git_commit": "b" * 7, "git_dirty": False})
    _refresh_component(source, "CV08")
    _expect("SOURCE_IDENTITY_MISMATCH", build_bundle, source, SHA)


def test_PE08_dirty_source_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); _write(source / "native/candidate/one/manifest.json", {"commit": SHA, "dirty": True})
    _refresh_component(source, "CV04")
    _expect("SOURCE_TREE_DIRTY", build_bundle, source, SHA)


def test_PE09_expected_sha_mismatch_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); _expect("EXPECTED_SHA_MISMATCH", build_bundle, source, OTHER_SHA)


def test_PE10_symlink_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path / "symlink", monkeypatch); os.symlink(source / "result.json", source / "linked.json")
    _expect("EVIDENCE_NODE_FORBIDDEN", build_bundle, source, SHA)
    source = _run(tmp_path / "fifo", monkeypatch); os.mkfifo(source / "blocked.log")
    _expect("EVIDENCE_NODE_FORBIDDEN", build_bundle, source, SHA)


def test_PE11_unknown_type_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); (source / "binary.bin").write_bytes(b"safe")
    _expect("EVIDENCE_TYPE_FORBIDDEN", build_bundle, source, SHA)


def test_PE12_oversize_file_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); (source / "large.log").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    _expect("EVIDENCE_FILE_TOO_LARGE", build_bundle, source, SHA)


def test_PE13_uploaded_privacy_canary_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); _write(source / "teacher@district.edu.json", {"raw": "not copied"})
    _expect("EVIDENCE_PRIVACY_REJECTED", build_bundle, source, SHA)


def test_PE14_manifest_tamper_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); bundle = build_bundle(source, SHA)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["components"][1]["id"] = "CV01"
    manifest_bytes = evidence_module.canonical_json(manifest)
    (bundle / "manifest.json").write_bytes(manifest_bytes)
    (bundle / "manifest.sha256").write_text(hashlib.sha256(manifest_bytes).hexdigest() + "  manifest.json\n")
    _expect("EVIDENCE_INVALID", verify_bundle, bundle)


def test_PA01_matching_stage_request_passes(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA)
    assert _eligible(source, _promotion_files(source, tmp_path))["status"] == "eligible"


def test_PA02_wrong_actor_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[0].read_text()); payload["actor_login"] = "intruder"; _write(paths[0], payload)
    _expect("ACTOR_MISMATCH", _eligible, source, paths)


def test_PA03_triggering_actor_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[2].read_text()); payload["triggering_actor"] = "intruder"; _write(paths[2], payload)
    _expect("TRIGGERING_ACTOR_MISMATCH", _eligible, source, paths)


def test_PA04_rerun_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[1].read_text()); payload["workflow_run"]["run_attempt"] = 2; _write(paths[1], payload)
    _expect("RUN_ATTEMPT_REJECTED", _eligible, source, paths)


def test_PA05_non_manual_event_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[1].read_text()); payload["workflow_run"]["event"] = "push"; _write(paths[1], payload)
    _expect("EVENT_REJECTED", _eligible, source, paths)


def test_PA06_unsuccessful_validation_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[1].read_text()); payload["workflow_run"]["conclusion"] = "failure"; _write(paths[1], payload)
    _expect("VALIDATION_NOT_SUCCESSFUL", _eligible, source, paths)


def test_PA07_candidate_mismatch_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[0].read_text()); payload["candidate_sha"] = OTHER_SHA; payload["candidate_ref"] = f"release-candidate/{OTHER_SHA}"; _write(paths[0], payload)
    _expect("CANDIDATE_MISMATCH", _eligible, source, paths)


def test_PA08_evidence_digest_mismatch_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[2].read_text()); payload["evidence_sha256"] = "f" * 64; _write(paths[2], payload)
    _expect("EVIDENCE_DIGEST_MISMATCH", _eligible, source, paths)


def test_PA09_workflow_identity_mismatch_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[1].read_text()); payload["workflow_run"]["path"] = ".github/workflows/other.yml@main"; _write(paths[1], payload)
    _expect("WORKFLOW_IDENTITY_MISMATCH", _eligible, source, paths)
    paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[0].read_text()); payload["promotion_workflow_sha"] = "f" * 40; _write(paths[0], payload)
    _expect("WORKFLOW_IDENTITY_MISMATCH", _eligible, source, paths)


def test_PA10_artifact_mismatch_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[1].read_text()); payload["artifact"]["digest"] = "sha256:" + "f" * 64; _write(paths[1], payload)
    _expect("ARTIFACT_MISMATCH", _eligible, source, paths)
    paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[1].read_text()); payload["artifact"]["workflow_run"]["id"] = 999; _write(paths[1], payload)
    _expect("ARTIFACT_MISMATCH", _eligible, source, paths)


def test_PA11_expired_request_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[0].read_text()); payload["expires_at"] = "2026-09-15T11:59:00Z"; _write(paths[0], payload)
    _expect("AUTHORIZATION_TIME_INVALID", _eligible, source, paths)


def test_PA12_scope_mismatch_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[2].read_text()); payload["environment"] = "demo"; _write(paths[2], payload)
    _expect("AUTHORIZATION_SCOPE_MISMATCH", _eligible, source, paths)


def test_PA13_verbatim_text_hash_mismatch_rejected(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path)
    payload = json.loads(paths[0].read_text()); payload["authorization_text"] += " changed"; _write(paths[0], payload)
    _expect("AUTHORIZATION_TEXT_MISMATCH", _eligible, source, paths)


def test_PA14_production_requires_exact_approval(tmp_path, monkeypatch):
    source = _run(tmp_path, monkeypatch); build_bundle(source, SHA); paths = _promotion_files(source, tmp_path, "production")
    payload = json.loads(paths[2].read_text()); payload["environment_review"] = None; _write(paths[2], payload)
    _expect("PRODUCTION_APPROVAL_MISSING", _eligible, source, paths)
    paths = _promotion_files(source, tmp_path, "production")
    assert _eligible(source, paths)["action"] == "production"


def _workflow() -> str:
    return (Path(__file__).parents[2] / ".github/workflows/trusted-validation.yml").read_text()


def _workflow_data() -> dict:
    return yaml.load(_workflow(), Loader=yaml.BaseLoader)


def test_PW01_evidence_between_aggregate_and_upload():
    source = _workflow(); assert source.index("make trusted-validation-gate") < source.index("make practical-evidence") < source.index("actions/upload-artifact")


def test_PW02_evidence_receives_exact_run_and_sha():
    assert 'SOURCE_RUN_DIR="artifacts/trusted-validation/${RELEASE_CONTROL_RUN_ID}" EXPECTED_SHA="$TARGET_SHA"' in _workflow()


def test_PW03_upload_is_always_and_evidence_only():
    upload = _workflow_data()["jobs"]["validate"]["steps"][-1]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "artifacts/trusted-validation/${{ env.RELEASE_CONTROL_RUN_ID }}/evidence/"


def test_PW04_evidence_is_not_continue_on_error():
    source = _workflow(); section = source[source.index("name: Build practical release evidence"):source.index("name: Upload sanitized validation evidence")]
    assert "continue-on-error" not in section


def test_PW05_workflow_uses_established_trusted_m4_boundary():
    workflow = _workflow_data(); job = workflow["jobs"]["validate"]
    assert set(workflow["on"]) == {"workflow_dispatch"} and workflow["permissions"] == {}
    assert job["permissions"] == {"contents": "read"}
    assert job["runs-on"] == ["self-hosted", "mac-mini-runner", "m4"]
    assert "isolated" not in job["name"].lower()
    identity = job["steps"][0]["run"]
    assert "test -x /Users/ollama/homebrew/bin/python3.11" in identity
    assert "test -x /opt/homebrew/bin/npm" in identity
    assert "GITHUB_PATH" in identity
    assert "PLAYWRIGHT_SKIP_BROWSER_GC=1 python3.11 -m playwright install chromium" in _workflow()
    assert job["steps"][1]["uses"] == "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
    assert job["steps"][1]["with"]["persist-credentials"] == "false"
    assert job["steps"][-1]["uses"] == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_PW06_no_mutation_surface_added():
    workflow = _workflow_data()
    source = json.dumps(workflow, sort_keys=True).lower()
    for forbidden in ("docker push", "git push", "git tag", "gh release", "environment:", "secrets."):
        assert forbidden not in source
