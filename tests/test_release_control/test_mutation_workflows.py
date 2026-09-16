from __future__ import annotations

import importlib.util
import json
import subprocess
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from ops.release_control import mutation_workflow as mutation
from ops.release_control.practical_evidence import build_bundle, verify_bundle
from ops.release_control.promotion_eligibility import verify_eligibility
from ops.release_control import promotion_workflow

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
OTHER_SHA = "b" * 40
DIGEST = "sha256:" + "c" * 64
OTHER_DIGEST = "sha256:" + "d" * 64
EVIDENCE = "e" * 64
NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def _expect(code: str, function, *args, **kwargs):
    with pytest.raises(mutation.MutationError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def _inputs(kind: str = "publication") -> dict:
    payload = {
        "candidate_sha": SHA,
        "candidate_ref": f"release-candidate/{SHA}",
        "version": "1.9.13",
        "evidence_sha256": EVIDENCE,
        "validation_run_id": 100,
        "promotion_run_id": 200,
    }
    if kind == "production":
        payload.update({
            "publication_run_id": 300,
            "publication_receipt_sha256": "f" * 64,
            "image_digest": DIGEST,
        })
    return payload


def _current(kind: str = "publication") -> dict:
    return {
        "repository_id": 77, "repository": mutation.REPOSITORY, "run_id": 400,
        "run_attempt": 1, "actor_login": mutation.OWNER_LOGIN, "actor_id": mutation.OWNER_ID,
        "triggering_actor": mutation.OWNER_LOGIN, "event": "workflow_dispatch",
        "workflow_path": mutation.PUBLICATION_WORKFLOW if kind == "publication" else mutation.PRODUCTION_WORKFLOW,
        "workflow_sha": "9" * 40,
    }


def _run_payload(path: str, run_id: int, artifact_name: str) -> tuple[dict, dict]:
    run = {
        "id": run_id, "run_attempt": 1, "event": "workflow_dispatch", "conclusion": "success",
        "path": path, "head_branch": "main", "head_sha": "8" * 40,
        "actor": {"login": mutation.OWNER_LOGIN, "id": mutation.OWNER_ID},
        "triggering_actor": {"login": mutation.OWNER_LOGIN},
        "repository": {"id": 77, "full_name": mutation.REPOSITORY},
    }
    artifact = {
        "total_count": 1,
        "artifacts": [{
            "id": 123, "name": artifact_name, "expired": False, "digest": "sha256:" + "7" * 64,
            "workflow_run": {"id": run_id, "repository_id": 77, "head_repository_id": 77, "head_sha": "8" * 40},
        }],
    }
    return run, artifact


def test_MW01_dispatch_validation_accepts_exact_publication_and_production():
    mutation.validate_dispatch(_inputs(), _current(), kind="publication")
    mutation.validate_dispatch(_inputs("production"), _current("production"), kind="production")


@pytest.mark.parametrize("key,value,code", [
    ("actor_login", "attacker", "ACTOR_MISMATCH"),
    ("triggering_actor", "attacker", "TRIGGERING_ACTOR_MISMATCH"),
    ("run_attempt", 2, "RUN_IDENTITY_REJECTED"),
    ("event", "push", "RUN_IDENTITY_REJECTED"),
    ("workflow_path", ".github/workflows/other.yml", "WORKFLOW_IDENTITY_MISMATCH"),
])
def test_MW02_dispatch_identity_rejections(key, value, code):
    current = _current(); current[key] = value
    _expect(code, mutation.validate_dispatch, _inputs(), current, kind="publication")


@pytest.mark.parametrize("key,value,code", [
    ("candidate_sha", "A" * 40, "CANDIDATE_MISMATCH"),
    ("candidate_ref", f"release-candidate/{OTHER_SHA}", "CANDIDATE_MISMATCH"),
    ("version", "01.9.13", "VERSION_INVALID"),
    ("evidence_sha256", "x" * 64, "EVIDENCE_DIGEST_MISMATCH"),
    ("validation_run_id", 0, "INPUT_INVALID"),
])
def test_MW03_dispatch_input_rejections(key, value, code):
    inputs = _inputs(); inputs[key] = value
    _expect(code, mutation.validate_dispatch, inputs, _current(), kind="publication")


def test_MW04_exact_artifact_observation_and_binding():
    run, artifacts = _run_payload(mutation.PROMOTION_WORKFLOW, 200, "odin-promotion-eligibility-200-1")
    observed, artifact_id = mutation.select_artifact(
        run, artifacts, run_id=200, workflow_path=mutation.PROMOTION_WORKFLOW,
        artifact_name="odin-promotion-eligibility-200-1",
    )
    assert artifact_id == 123 and observed["head_sha"] == "8" * 40
    artifacts["artifacts"][0]["workflow_run"]["head_sha"] = OTHER_SHA
    _expect(
        "ARTIFACT_MISMATCH", mutation.select_artifact, run, artifacts, run_id=200,
        workflow_path=mutation.PROMOTION_WORKFLOW, artifact_name="odin-promotion-eligibility-200-1",
    )


def _promotion_archive(tmp_path: Path, monkeypatch, action: str) -> tuple[Path, Path, str]:
    practical_path = ROOT / "tests/test_release_control/test_practical_evidence.py"
    practical_spec = importlib.util.spec_from_file_location("practical_fixture_mutation", practical_path)
    assert practical_spec and practical_spec.loader
    practical = importlib.util.module_from_spec(practical_spec); practical_spec.loader.exec_module(practical)
    source = practical._run(tmp_path, monkeypatch)
    evidence_dir = build_bundle(source, SHA)
    evidence_digest = verify_bundle(evidence_dir)["evidence_sha256"]
    promotion_path = ROOT / "tests/test_release_control/test_promotion_workflow.py"
    promotion_spec = importlib.util.spec_from_file_location("promotion_fixture_mutation", promotion_path)
    assert promotion_spec and promotion_spec.loader
    fixture = importlib.util.module_from_spec(promotion_spec); promotion_spec.loader.exec_module(fixture)
    inputs = fixture._inputs(action); inputs["evidence_sha256"] = evidence_digest
    approval = fixture._approval() if action == "production" else []
    request, context = promotion_workflow.build_records(
        inputs, fixture._current(action), fixture._observation(), approval, now=fixture.NOW,
    )
    records = tmp_path / f"records-{action}"; records.mkdir()
    paths = {
        "request.json": request,
        "validation-observation.json": fixture._observation(),
        "promotion-context.json": context,
    }
    for name, payload in paths.items():
        (records / name).write_text(json.dumps(payload), encoding="utf-8")
    decision = verify_eligibility(
        evidence_dir, records / "request.json", records / "validation-observation.json",
        records / "promotion-context.json", now=fixture.NOW,
    )
    (records / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    (records / "index.html").write_text("<!doctype html><title>decision</title>", encoding="utf-8")
    archive = tmp_path / f"promotion-{action}.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in sorted(mutation.PROMOTION_FILES):
            output.write(records / name, name)
    return archive, evidence_dir, evidence_digest


def test_MW05_recomputes_stage_and_production_promotion(monkeypatch, tmp_path):
    for action in ("stage", "production"):
        archive, evidence_dir, digest = _promotion_archive(tmp_path / action, monkeypatch, action)
        result = mutation.verify_promotion_artifact(
            archive, tmp_path / f"verified-{action}", evidence_dir, expected_action=action,
            expected_candidate=SHA, expected_evidence=digest, expected_run_id=200, now=NOW,
        )
        assert result["status"] == "eligible" and result["action"] == action


def test_MW06_archive_rejects_paths_and_unexpected_members(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in mutation.PROMOTION_FILES:
            output.writestr("../decision.json" if name == "decision.json" else name, b"x")
    _expect(
        "ARTIFACT_INVALID", mutation.verify_promotion_artifact, archive, tmp_path / "out",
        tmp_path / "evidence", expected_action="stage", expected_candidate=SHA,
        expected_evidence=EVIDENCE, expected_run_id=200,
    )


class FakeRegistry:
    def __init__(self, values: dict[str, str]):
        self.values = dict(values)
        self.commands: list[list[str]] = []
        self.fail_copy_target: str | None = None
        self.fail_after_copy_target: str | None = None

    def __call__(self, command: Sequence[str], **_kwargs):
        command = list(command); self.commands.append(command)
        if command[3] == "inspect":
            reference = command[4]
            digest = reference.rsplit("@", 1)[1] if "@" in reference else self.values.get(reference)
            if not digest:
                return subprocess.CompletedProcess(command, 1, "", "manifest unknown")
            body = {"schemaVersion": 2, "digest": digest, "manifests": []}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")
        target = command[command.index("--tag") + 1]; source = command[-1]
        if self.fail_copy_target == target:
            return subprocess.CompletedProcess(command, 1, "", "injected")
        digest = source.rsplit("@", 1)[1]
        self.values[target] = digest
        if self.fail_after_copy_target == target:
            return subprocess.CompletedProcess(command, 1, "", "injected after copy")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_MW07_immutable_attach_write_noop_and_conflict():
    repository = "localhost:5000/odin"
    registry = FakeRegistry({f"{repository}@{DIGEST}": DIGEST})
    written = mutation.attach_tag(repository=repository, target_tag=f"sha-{SHA}", source_digest=DIGEST, runner=registry)
    assert written["status"] == "written"
    noop = mutation.attach_tag(repository=repository, target_tag=f"sha-{SHA}", source_digest=DIGEST, runner=registry)
    assert noop["status"] == "noop"
    registry.values[f"{repository}:v1.9.13"] = OTHER_DIGEST
    _expect("TAG_CONFLICT", mutation.attach_tag, repository=repository, target_tag="v1.9.13", source_digest=DIGEST, runner=registry)


def test_MW08_latest_move_creates_durable_rollback_first():
    repository = "localhost:5000/odin"
    registry = FakeRegistry({f"{repository}:latest": OTHER_DIGEST, f"{repository}@{DIGEST}": DIGEST})
    writes = mutation.move_latest(
        repository=repository, target_digest=DIGEST, rollback_tag="rollback-400-1", runner=registry,
    )
    assert [row["tag"] for row in writes] == ["rollback-400-1", "latest"]
    assert registry.values[f"{repository}:rollback-400-1"] == OTHER_DIGEST
    assert registry.values[f"{repository}:latest"] == DIGEST
    copy_targets = [item[item.index("--tag") + 1] for item in registry.commands if "create" in item]
    assert copy_targets == [f"{repository}:rollback-400-1", f"{repository}:latest"]


def test_MW09_latest_move_fails_before_mutation_without_prior():
    registry = FakeRegistry({})
    _expect(
        "PRIOR_LATEST_MISSING", mutation.move_latest, repository="localhost:5000/odin",
        target_digest=DIGEST, rollback_tag="rollback-400-1", runner=registry,
    )
    assert not any("create" in item for item in registry.commands)


def test_MW09b_rollback_collision_stops_before_latest_write():
    repository = "localhost:5000/odin"
    registry = FakeRegistry({f"{repository}:latest": OTHER_DIGEST, f"{repository}:rollback-400-1": DIGEST})
    _expect(
        "TAG_CONFLICT", mutation.move_latest, repository=repository, target_digest=DIGEST,
        rollback_tag="rollback-400-1", runner=registry,
    )
    assert registry.values[f"{repository}:latest"] == OTHER_DIGEST


def _publication_state() -> dict:
    return {
        "schema_version": 1, "kind": "publication", "status": "success", "phase": "registry_verified",
        "repository": mutation.REPOSITORY, "repository_id": 77, "run_id": 400, "run_attempt": 1,
        "workflow_sha": "9" * 40, "actor_login": mutation.OWNER_LOGIN,
        "candidate_sha": SHA, "candidate_ref": f"release-candidate/{SHA}",
        "validation_run_id": 100, "promotion_run_id": 200, "evidence_sha256": EVIDENCE,
        "version": "1.9.13", "image_repository": mutation.IMAGE_REPOSITORY,
        "staging_tag": f"candidate-{SHA}-400-1", "sha_tag": f"sha-{SHA}", "version_tag": "v1.9.13",
        "target_digest": DIGEST,
        "platform_manifests": [
            {"os": "linux", "architecture": "amd64", "digest": "sha256:" + "1" * 64, "status": "passed"},
            {"os": "linux", "architecture": "arm64", "digest": "sha256:" + "2" * 64, "status": "passed"},
        ],
        "tag_writes": [
            {"tag": f"candidate-{SHA}-400-1", "before": None, "after": DIGEST, "status": "written"},
            {"tag": f"sha-{SHA}", "before": None, "after": DIGEST, "status": "written"},
            {"tag": "v1.9.13", "before": None, "after": DIGEST, "status": "written"},
        ],
        "started_at": "2026-09-15T12:00:00Z", "updated_at": "2026-09-15T12:05:00Z",
        "tool_versions": {"python": "3.11.15", "docker": "29.4.0", "buildx": "v0.33.0"},
        "error": None, "cleanup": {"registry_logout": True, "auth_removed": True},
    }


def test_MW10_receipt_round_trip_and_tamper_detection(tmp_path):
    output = tmp_path / "receipt"
    rendered = mutation.render_receipt(_publication_state(), output)
    archive = tmp_path / "receipt.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for name in sorted(mutation.RECEIPT_FILES): zipped.write(output / name, name)
    verified = mutation.verify_receipt_bundle(
        archive, tmp_path / "verified", expected_kind="publication",
        expected_sha256=rendered["receipt_sha256"],
    )
    assert verified["target_digest"] == DIGEST
    _expect(
        "RECEIPT_DIGEST_MISMATCH", mutation.verify_receipt_bundle, archive,
        tmp_path / "wrong", expected_kind="publication", expected_sha256="0" * 64,
    )


def test_MW11_state_machine_rejects_phase_regression():
    state = {"phase": "tag_written"}
    _expect("STATE_INVALID", mutation.transition_state, state, {"phase": "mutation_started"})


def test_MW12_semantics_reject_fabricated_success_and_accept_truthful_failure(tmp_path):
    forged = _publication_state(); forged["phase"] = "mutation_started"; forged["tag_writes"] = []
    _expect("RECEIPT_INVALID", mutation.validate_receipt, forged)
    forged_production = {
        "schema_version": 1, "kind": "production", "status": "success", "phase": "mutation_started",
        "repository": mutation.REPOSITORY, "repository_id": 77, "run_id": 400, "run_attempt": 1,
        "workflow_sha": "9" * 40, "actor_login": mutation.OWNER_LOGIN, "candidate_sha": SHA,
        "candidate_ref": f"release-candidate/{SHA}", "validation_run_id": 100, "promotion_run_id": 200,
        "publication_run_id": 300, "publication_receipt_sha256": "f" * 64, "evidence_sha256": EVIDENCE,
        "version": "1.9.13", "image_repository": mutation.IMAGE_REPOSITORY, "target_digest": DIGEST,
        "prior_latest_digest": OTHER_DIGEST, "rollback_tag": "rollback-400-1", "rollback_command": "restore",
        "observed_latest_digest": None, "tag_writes": [],
        "public_health": {"url": "https://odin.subsystem.app/health", "tls_valid": False, "status": None, "version": None, "ready_observation": "not_checked"},
        "started_at": "2026-09-15T12:00:00Z", "updated_at": "2026-09-15T12:00:00Z",
        "tool_versions": {"python": "3.11.15", "docker": "29.4.0", "buildx": "v0.33.0"},
        "error": {"code": "INCOMPLETE", "detail": "not promoted"},
        "cleanup": {"registry_logout": True, "auth_removed": True},
    }
    _expect("RECEIPT_INVALID", mutation.validate_receipt, forged_production)
    good_production = dict(forged_production)
    good_production.update({
        "phase": "production_observed", "observed_latest_digest": DIGEST, "error": None,
        "tag_writes": [
            {"tag": "rollback-400-1", "before": None, "after": OTHER_DIGEST, "status": "written"},
            {"tag": "latest", "before": OTHER_DIGEST, "after": DIGEST, "status": "written"},
        ],
        "public_health": {"url": "https://odin.subsystem.app/health", "tls_valid": True, "status": "ok", "version": "1.9.13", "ready_observation": "ready_true"},
    })
    mutation.validate_receipt(good_production)

    publication_with_extra_write = _publication_state()
    publication_with_extra_write["tag_writes"].append({
        "tag": "unexpected", "before": None, "after": DIGEST, "status": "attempted",
    })
    _expect("RECEIPT_INVALID", mutation.validate_receipt, publication_with_extra_write)

    production_with_extra_write = dict(good_production)
    production_with_extra_write["tag_writes"] = [
        *good_production["tag_writes"],
        {"tag": "unexpected", "before": None, "after": DIGEST, "status": "failed"},
    ]
    _expect("RECEIPT_INVALID", mutation.validate_receipt, production_with_extra_write)

    failed = _publication_state()
    failed.update({
        "status": "failed", "phase": "mutation_started", "target_digest": None,
        "platform_manifests": [],
        "tag_writes": [{"tag": failed["staging_tag"], "before": None, "after": None, "status": "failed"}],
        "error": {"code": "MUTATION_STEP_FAILED", "detail": "staging"},
    })
    mutation.validate_receipt(failed)
    mutation.render_receipt(failed, tmp_path / "failure-receipt")


@pytest.mark.parametrize("status,body,expected", [
    (200, b'{"ready":true,"version":"1.9.13"}', "ready_true"),
    (401, b"auth", "perimeter_401"),
    (403, b"auth", "perimeter_403"),
])
def test_MW13_readiness_accepts_only_ready_or_explicit_perimeter(status, body, expected):
    assert mutation.validate_readiness(status, body, "1.9.13") == expected


@pytest.mark.parametrize("status,body", [
    (200, b'{"ready":false,"version":"1.9.13"}'),
    (200, b'{"ready":true,"version":"1.9.12"}'),
    (404, b"missing"), (503, b'{"detail":{"ready":false}}'),
])
def test_MW14_readiness_rejects_false_wrong_version_and_server_errors(status, body):
    _expect("PRODUCTION_NOT_READY", mutation.validate_readiness, status, body, "1.9.13")


@pytest.mark.parametrize("tag", [
    f"candidate-{SHA}-400-1", f"sha-{SHA}", "v1.9.13", "rollback-400-1", "latest",
])
def test_MW15_failure_capture_reconciles_write_that_reached_registry(tag):
    repository = "localhost:5000/odin"
    if tag.startswith("rollback") or tag == "latest":
        state = {
            "schema_version": 1, "kind": "production", "status": "failed", "phase": "tag_written",
            "repository": mutation.REPOSITORY, "repository_id": 77, "run_id": 400, "run_attempt": 1,
            "workflow_sha": "9" * 40, "actor_login": mutation.OWNER_LOGIN, "candidate_sha": SHA,
            "candidate_ref": f"release-candidate/{SHA}", "validation_run_id": 100, "promotion_run_id": 200,
            "publication_run_id": 300, "publication_receipt_sha256": "f" * 64, "evidence_sha256": EVIDENCE,
            "version": "1.9.13", "image_repository": repository, "target_digest": DIGEST,
            "prior_latest_digest": OTHER_DIGEST, "rollback_tag": "rollback-400-1", "rollback_command": "restore",
            "observed_latest_digest": None, "tag_writes": [],
            "public_health": {"url": "https://odin.subsystem.app/health", "tls_valid": True, "status": None, "version": None, "ready_observation": "not_checked"},
            "started_at": "2026-09-15T12:00:00Z", "updated_at": "2026-09-15T12:00:00Z",
            "tool_versions": {"python": "3.11.15", "docker": "29.4.0", "buildx": "v0.33.0"},
            "error": {"code": "INCOMPLETE", "detail": "pending"},
            "cleanup": {"registry_logout": False, "auth_removed": False},
        }
    else:
        state = _publication_state(); state.update({"status": "failed", "phase": "tag_written", "error": {"code": "INCOMPLETE", "detail": "pending"}})
    state["image_repository"] = repository
    after = OTHER_DIGEST if tag.startswith("rollback") else DIGEST
    before = OTHER_DIGEST if tag == "latest" else None
    state["tag_writes"] = [{"tag": tag, "before": before, "after": after, "status": "attempted"}]
    captured = mutation.capture_failure(state, runner=FakeRegistry({f"{repository}:{tag}": after}))
    assert captured["status"] == "failed" and captured["tag_writes"][0]["status"] == "written"
    if tag == "latest":
        assert captured["observed_latest_digest"] == DIGEST


def test_MW15b_injected_post_copy_failure_is_recoverable_in_receipt_state():
    repository = "localhost:5000/odin"; target = f"{repository}:v1.9.13"
    registry = FakeRegistry({}); registry.fail_after_copy_target = target
    _expect(
        "REGISTRY_COPY_FAILED", mutation.attach_tag, repository=repository,
        target_tag="v1.9.13", source_digest=DIGEST, runner=registry,
    )
    state = _publication_state(); state.update({"status": "failed", "phase": "tag_written", "image_repository": repository, "error": {"code": "INCOMPLETE", "detail": "pending"}})
    state["tag_writes"] = [{"tag": "v1.9.13", "before": None, "after": DIGEST, "status": "attempted"}]
    captured = mutation.capture_failure(state, runner=registry)
    assert captured["tag_writes"] == [{"tag": "v1.9.13", "before": None, "after": DIGEST, "status": "written"}]


def test_MW15c_archive_rejects_duplicate_symlink_and_oversize(tmp_path):
    for mode in ("duplicate", "symlink", "oversize"):
        archive = tmp_path / f"{mode}.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for name in sorted(mutation.RECEIPT_FILES):
                if mode == "symlink" and name == "index.html":
                    info = zipfile.ZipInfo(name); info.external_attr = (0o120777 << 16); output.writestr(info, "target")
                elif mode == "oversize" and name == "index.html":
                    output.writestr(name, b"x" * (mutation.MAX_FILE_BYTES + 1))
                else:
                    output.writestr(name, b"x")
            if mode == "duplicate":
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    output.writestr("index.html", b"duplicate")
        _expect(
            "ARTIFACT_INVALID", mutation.verify_receipt_bundle, archive, tmp_path / f"out-{mode}",
            expected_kind="publication", expected_sha256="0" * 64,
        )


def _workflow(name: str) -> tuple[str, dict]:
    source = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    return source, yaml.safe_load(source)


def test_MW16_workflows_are_manual_serial_and_least_privilege():
    publication_source, publication = _workflow("publish-image.yml")
    production_source, production = _workflow("promote-production.yml")
    for source, payload in ((publication_source, publication), (production_source, production)):
        assert "push:" not in source and "pull_request:" not in source and "workflow_dispatch:" in source
        assert payload["permissions"] == {}
        assert payload["concurrency"] == {"group": "odin-release-mutation", "cancel-in-progress": False}
        assert "runs-on: [self-hosted, mac-mini-runner, m4]" in source
    assert publication["jobs"]["publish"]["permissions"] == {"actions": "read", "contents": "read", "packages": "write"}
    assert production["jobs"]["promote"]["permissions"] == {"actions": "read", "contents": "read", "packages": "write"}
    assert production["jobs"]["promote"]["environment"] == "production"


def test_MW17_workflow_side_effect_boundaries_and_pins():
    publication_source, publication = _workflow("publish-image.yml")
    production_source, production = _workflow("promote-production.yml")
    assert ":latest" not in publication_source
    assert "docker buildx build" in publication_source and "--platform linux/amd64,linux/arm64" in publication_source
    assert "docker buildx build" not in production_source and "actions/checkout" not in production_source
    for payload in (publication, production):
        for step in next(iter(payload["jobs"].values()))["steps"]:
            if "uses" in step:
                assert "@" in step["uses"] and len(step["uses"].rsplit("@", 1)[1]) == 40
    forbidden = ("ssh ", "kubectl", "helm ", "docker compose", "docker-compose")
    assert all(item not in production_source.lower() for item in forbidden)


def test_MW18_workflow_inputs_never_interpolate_into_shell():
    for name in ("publish-image.yml", "promote-production.yml"):
        _, payload = _workflow(name)
        job = next(iter(payload["jobs"].values()))
        for step in job["steps"]:
            if "run" in step:
                assert "${{ inputs." not in step["run"]


def test_MW19_failure_receipts_and_fresh_authorization_are_wired():
    publication_source, publication = _workflow("publish-image.yml")
    production_source, production = _workflow("promote-production.yml")
    assert "RECEIPT_DIR=$root/receipt" in publication_source and ".odin-publication-receipt" not in publication_source
    assert "RECEIPT_DIR=$root/receipt" in production_source and ".odin-production-receipt" not in production_source
    assert "capture-failure --state" in publication_source and "capture-failure --state" in production_source
    assert production_source.count("-m ops.release_control.promotion_eligibility") >= 2
    assert "verify-readiness --status-code" in production_source

    publication_steps = {step["name"]: step for step in publication["jobs"]["publish"]["steps"]}
    publication_failure = publication_steps["Capture truthful publication failure state"]
    for forbidden in ("attach-tag", "replace-latest", "imagetools create", "docker buildx build"):
        assert forbidden not in publication_failure["run"]
    assert publication_failure["if"] == "failure() && env.MUTATION_STARTED == '1'"
    assert "attach-tag" in publication_steps["Attach collision-safe immutable tags"]["run"]
    assert "attach-tag" in publication_steps["Attach collision-safe version tag"]["run"]
    assert "if" not in publication_steps["Attach collision-safe immutable tags"]
    assert "if" not in publication_steps["Attach collision-safe version tag"]

    production_steps = {step["name"]: step for step in production["jobs"]["promote"]["steps"]}
    production_failure = production_steps["Capture truthful production failure state"]
    for forbidden in ("attach-tag", "replace-latest", "imagetools create", "docker buildx build"):
        assert forbidden not in production_failure["run"]
    assert production_failure["if"] == "failure() && env.MUTATION_STARTED == '1'"
    assert "PRODUCTION_OBSERVATION_FAILED" in production_steps["Observe production version over verified TLS"]["run"]


def test_MW20_publication_bootstrap_finds_runner_docker_before_path_export():
    _, publication = _workflow("publish-image.yml")
    steps = {step["name"]: step for step in publication["jobs"]["publish"]["steps"]}
    bootstrap = steps["Establish isolated publication root"]["run"]
    docker_check = "test -x /Users/ollama/homebrew/bin/docker"
    path_export = "printf '%s\\n' /Users/ollama/homebrew/bin /opt/homebrew/bin >> \"$GITHUB_PATH\""
    assert docker_check in bootstrap
    assert path_export in bootstrap
    assert bootstrap.index(docker_check) < bootstrap.index(path_export)
