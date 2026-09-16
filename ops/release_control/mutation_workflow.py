"""Fail-closed helpers for ODIN image publication and production promotion."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from jsonschema import Draft202012Validator, FormatChecker

from .practical_evidence import MAX_FILE_BYTES, canonical_json
from .promotion_eligibility import verify_eligibility

OWNER_LOGIN = "HughKantsime"
OWNER_ID = 201174638
REPOSITORY = "HughKantsime/runsodin"
IMAGE_REPOSITORY = "ghcr.io/hughkantsime/odin"
PROMOTION_WORKFLOW = ".github/workflows/promote.yml"
PUBLICATION_WORKFLOW = ".github/workflows/publish-image.yml"
PRODUCTION_WORKFLOW = ".github/workflows/promote-production.yml"
PROMOTION_FILES = frozenset({
    "request.json", "validation-observation.json", "promotion-context.json",
    "decision.json", "index.html",
})
RECEIPT_FILES = frozenset({"receipt.json", "receipt.sha256", "index.html"})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PHASES = ("preflight", "mutation_started", "tag_written", "registry_verified", "production_observed")
TERMINAL = frozenset({"success", "failed"})


class MutationError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise MutationError(code, detail)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MutationError("JSON_INVALID", str(path)) from exc


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _atomic_json(path: Path, payload: object) -> None:
    _atomic_bytes(path, canonical_json(payload))


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise MutationError("TIME_INVALID", str(value)) from exc
    if parsed.tzinfo is None:
        _fail("TIME_INVALID", str(value))
    return parsed.astimezone(timezone.utc)


def _validate_schema(payload: dict[str, Any], filename: str) -> None:
    schema = json.loads(Path(__file__).with_name(filename).read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda item: list(item.path),
    )
    if errors:
        _fail("RECEIPT_INVALID", errors[0].message)


def validate_receipt(payload: dict[str, Any]) -> None:
    kind = payload.get("kind")
    if kind not in {"publication", "production"}:
        _fail("RECEIPT_INVALID", "unknown kind")
    _validate_schema(payload, f"{kind}_receipt.schema.json")
    writes = payload["tag_writes"]
    tags = [item["tag"] for item in writes]
    if len(tags) != len(set(tags)):
        _fail("RECEIPT_INVALID", "duplicate tag-write records")
    if payload["status"] == "failed":
        if not isinstance(payload.get("error"), dict):
            _fail("RECEIPT_INVALID", "failed receipt must contain an error")
        return
    if payload.get("error") is not None or not all(payload["cleanup"].values()):
        _fail("RECEIPT_INVALID", "successful receipt requires null error and completed cleanup")
    target = payload["target_digest"]
    completed = {item["tag"]: item for item in writes if item["status"] in {"written", "noop"}}
    if kind == "publication":
        expected_tags = {payload["staging_tag"], payload["sha_tag"], payload["version_tag"]}
        platforms = {(item["os"], item["architecture"]): item for item in payload["platform_manifests"]}
        if payload["phase"] != "registry_verified" or payload["sha_tag"] != f"sha-{payload['candidate_sha']}" \
                or payload["version_tag"] != f"v{payload['version']}" \
                or payload["staging_tag"] != f"candidate-{payload['candidate_sha']}-{payload['run_id']}-{payload['run_attempt']}" \
                or set(tags) != expected_tags or len(writes) != len(expected_tags) \
                or set(completed) != expected_tags \
                or any(item["after"] != target for item in completed.values()) \
                or set(platforms) != {("linux", "amd64"), ("linux", "arm64")} \
                or any(item["status"] != "passed" for item in platforms.values()):
            _fail("RECEIPT_INVALID", "publication success invariants failed")
    else:
        expected_tags = {payload["rollback_tag"], "latest"}
        health = payload["public_health"]
        if payload["phase"] != "production_observed" or payload["rollback_tag"] != f"rollback-{payload['run_id']}-{payload['run_attempt']}" \
                or set(tags) != expected_tags or len(writes) != len(expected_tags) \
                or set(completed) != expected_tags or completed[payload["rollback_tag"]]["after"] != payload["prior_latest_digest"] \
                or completed["latest"]["before"] != payload["prior_latest_digest"] \
                or completed["latest"]["after"] != target or payload["observed_latest_digest"] != target \
                or health["tls_valid"] is not True or health["status"] != "ok" \
                or health["version"] != payload["version"] \
                or health["ready_observation"] not in {"ready_true", "perimeter_401", "perimeter_403"}:
            _fail("RECEIPT_INVALID", "production success invariants failed")


def validate_dispatch(inputs: dict[str, Any], current: dict[str, Any], *, kind: str) -> None:
    workflow = PUBLICATION_WORKFLOW if kind == "publication" else PRODUCTION_WORKFLOW
    if kind not in {"publication", "production"}:
        _fail("INPUT_INVALID", kind)
    if current.get("actor_login") != OWNER_LOGIN or current.get("actor_id") != OWNER_ID:
        _fail("ACTOR_MISMATCH", "exact owner identity required")
    if current.get("triggering_actor") != OWNER_LOGIN:
        _fail("TRIGGERING_ACTOR_MISMATCH", "exact owner trigger required")
    if current.get("event") != "workflow_dispatch" or current.get("run_attempt") != 1:
        _fail("RUN_IDENTITY_REJECTED", "manual first-attempt run required")
    if current.get("repository") != REPOSITORY or current.get("workflow_path") != workflow:
        _fail("WORKFLOW_IDENTITY_MISMATCH", "repository/workflow mismatch")
    if not isinstance(current.get("repository_id"), int) or current["repository_id"] < 1 \
            or not isinstance(current.get("run_id"), int) or current["run_id"] < 1 \
            or not isinstance(current.get("workflow_sha"), str) \
            or not SHA_RE.fullmatch(current["workflow_sha"]):
        _fail("WORKFLOW_IDENTITY_MISMATCH", "invalid run identity")
    candidate = inputs.get("candidate_sha")
    if not isinstance(candidate, str) or not SHA_RE.fullmatch(candidate) \
            or inputs.get("candidate_ref") != f"release-candidate/{candidate}":
        _fail("CANDIDATE_MISMATCH", "candidate SHA/ref mismatch")
    if not isinstance(inputs.get("version"), str) or not VERSION_RE.fullmatch(inputs["version"]):
        _fail("VERSION_INVALID", "strict X.Y.Z required")
    evidence = inputs.get("evidence_sha256")
    if not isinstance(evidence, str) or not HEX_DIGEST_RE.fullmatch(evidence):
        _fail("EVIDENCE_DIGEST_MISMATCH", "invalid evidence digest")
    for key in ("validation_run_id", "promotion_run_id"):
        value = inputs.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            _fail("INPUT_INVALID", key)
    if kind == "publication":
        recovery_run_id = inputs.get("recovery_publication_run_id")
        recovery_digest = inputs.get("recovery_receipt_sha256")
        if (recovery_run_id is None) != (recovery_digest is None):
            _fail("RECOVERY_INPUT_MISMATCH", "recovery run and receipt digest must be paired")
        if recovery_run_id is not None:
            if not isinstance(recovery_run_id, int) or isinstance(recovery_run_id, bool) \
                    or recovery_run_id < 1 or recovery_run_id == current["run_id"]:
                _fail("RECOVERY_RUN_INVALID", "prior publication run required")
            if not isinstance(recovery_digest, str) or not HEX_DIGEST_RE.fullmatch(recovery_digest):
                _fail("RECEIPT_DIGEST_MISMATCH", "invalid recovery receipt digest")
    if kind == "production":
        for key in ("publication_run_id",):
            value = inputs.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                _fail("INPUT_INVALID", key)
        if not isinstance(inputs.get("publication_receipt_sha256"), str) \
                or not HEX_DIGEST_RE.fullmatch(inputs["publication_receipt_sha256"]):
            _fail("RECEIPT_DIGEST_MISMATCH", "invalid publication receipt digest")
        if not isinstance(inputs.get("image_digest"), str) or not DIGEST_RE.fullmatch(inputs["image_digest"]):
            _fail("IMAGE_DIGEST_INVALID", "invalid OCI digest")


def select_artifact(
    workflow_run: dict[str, Any], artifact_payload: dict[str, Any], *, run_id: int,
    workflow_path: str, artifact_name: str, expected_conclusion: str = "success",
) -> tuple[dict[str, Any], int]:
    if workflow_run.get("id") != run_id or workflow_run.get("run_attempt") != 1:
        _fail("RUN_IDENTITY_REJECTED", "run ID/attempt mismatch")
    if expected_conclusion not in {"success", "failure"}:
        _fail("INPUT_INVALID", "unsupported expected conclusion")
    if workflow_run.get("event") != "workflow_dispatch" \
            or workflow_run.get("conclusion") != expected_conclusion:
        _fail("RUN_CONCLUSION_MISMATCH", f"manual {expected_conclusion} run required")
    if workflow_run.get("path") != workflow_path or workflow_run.get("head_branch") != "main":
        _fail("WORKFLOW_IDENTITY_MISMATCH", "workflow path/branch mismatch")
    if not isinstance(workflow_run.get("head_sha"), str) or not SHA_RE.fullmatch(workflow_run["head_sha"]):
        _fail("WORKFLOW_IDENTITY_MISMATCH", "invalid workflow head SHA")
    actor = workflow_run.get("actor", {})
    trigger = workflow_run.get("triggering_actor", {})
    repository = workflow_run.get("repository", {})
    if actor.get("login") != OWNER_LOGIN or actor.get("id") != OWNER_ID \
            or trigger.get("login") != OWNER_LOGIN:
        _fail("ACTOR_MISMATCH", "run owner identity mismatch")
    if repository.get("full_name") != REPOSITORY or not isinstance(repository.get("id"), int):
        _fail("WORKFLOW_IDENTITY_MISMATCH", "repository mismatch")
    artifacts = artifact_payload.get("artifacts")
    if artifact_payload.get("total_count") != 1 or not isinstance(artifacts, list) or len(artifacts) != 1:
        _fail("ARTIFACT_MISMATCH", "exactly one artifact required")
    artifact = artifacts[0]
    linked = artifact.get("workflow_run", {}) if isinstance(artifact, dict) else {}
    if not isinstance(artifact, dict) or artifact.get("name") != artifact_name \
            or artifact.get("expired") is not False or not isinstance(artifact.get("id"), int) \
            or not isinstance(artifact.get("digest"), str) \
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]) \
            or linked.get("id") != run_id or linked.get("repository_id") != repository["id"] \
            or linked.get("head_repository_id") != repository["id"] \
            or linked.get("head_sha") != workflow_run.get("head_sha"):
        _fail("ARTIFACT_MISMATCH", "artifact is not bound to exact run")
    observation = {
        "repository_id": repository["id"], "repository": repository["full_name"],
        "run_id": run_id, "run_attempt": 1, "actor_login": actor["login"],
        "actor_id": actor["id"], "triggering_actor": trigger["login"],
        "event": workflow_run["event"], "conclusion": workflow_run["conclusion"],
        "path": workflow_run["path"], "head_sha": workflow_run.get("head_sha"),
        "head_branch": workflow_run["head_branch"],
        "artifact": {"id": artifact["id"], "name": artifact["name"], "digest": artifact["digest"]},
    }
    return observation, artifact["id"]


def _extract_exact(zip_path: Path, output_dir: Path, expected: frozenset[str]) -> None:
    if output_dir.exists() or output_dir.is_symlink():
        _fail("ARTIFACT_INVALID", "output already exists")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            if len(members) != len(expected) or {item.filename for item in members} != expected:
                _fail("ARTIFACT_INVALID", "archive file set mismatch")
            total = 0
            for item in members:
                mode = item.external_attr >> 16
                total += item.file_size
                if item.file_size > MAX_FILE_BYTES or total > MAX_FILE_BYTES * len(expected) \
                        or "/" in item.filename or "\\" in item.filename or item.is_dir() \
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    _fail("ARTIFACT_INVALID", item.filename)
            temporary = Path(tempfile.mkdtemp(prefix=".mutation-artifact-", dir=output_dir.parent))
            try:
                for item in members:
                    (temporary / item.filename).write_bytes(archive.read(item))
                os.replace(temporary, output_dir)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise MutationError("ARTIFACT_INVALID", str(zip_path)) from exc


def verify_promotion_artifact(
    zip_path: Path, output_dir: Path, evidence_dir: Path, *, expected_action: str,
    expected_candidate: str, expected_evidence: str, expected_run_id: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    _extract_exact(zip_path, output_dir, PROMOTION_FILES)
    request = _read_json(output_dir / "request.json")
    context = _read_json(output_dir / "promotion-context.json")
    decision = _read_json(output_dir / "decision.json")
    if not all(isinstance(item, dict) for item in (request, context, decision)):
        _fail("PROMOTION_INVALID", "records must be objects")
    try:
        recomputed = verify_eligibility(
            evidence_dir, output_dir / "request.json", output_dir / "validation-observation.json",
            output_dir / "promotion-context.json", now=now,
        )
    except Exception as exc:
        raise MutationError(getattr(exc, "code", "PROMOTION_INVALID"), str(exc)) from exc
    if decision != recomputed:
        _fail("PROMOTION_DECISION_MISMATCH", "stored decision differs from recomputation")
    if request.get("action") != expected_action or request.get("candidate_sha") != expected_candidate \
            or request.get("evidence_sha256") != expected_evidence \
            or request.get("promotion_run_id") != expected_run_id \
            or context.get("run_id") != expected_run_id:
        _fail("PROMOTION_SCOPE_MISMATCH", "promotion does not match requested mutation")
    return recomputed


def verify_receipt_bundle(
    zip_path: Path, output_dir: Path, *, expected_kind: str, expected_sha256: str,
) -> dict[str, Any]:
    _extract_exact(zip_path, output_dir, RECEIPT_FILES)
    raw = (output_dir / "receipt.json").read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    declared = (output_dir / "receipt.sha256").read_text(encoding="ascii").strip()
    if actual != expected_sha256 or declared != actual:
        _fail("RECEIPT_DIGEST_MISMATCH", "receipt digest mismatch")
    payload = json.loads(raw)
    if payload.get("kind") != expected_kind:
        _fail("RECEIPT_INVALID", "receipt kind mismatch")
    validate_receipt(payload)
    return payload


def verify_publication_recovery_scope(
    receipt: dict[str, Any], inputs: dict[str, Any], *, prior_run_id: int,
    prior_workflow_sha: str,
) -> str:
    validate_receipt(receipt)
    if receipt.get("kind") != "publication" or receipt.get("status") != "success" \
            or receipt.get("run_id") != prior_run_id \
            or receipt.get("workflow_sha") != prior_workflow_sha:
        _fail("RECOVERY_RECEIPT_MISMATCH", "successful prior publication receipt required")
    expected = {
        "candidate_sha": inputs.get("candidate_sha"),
        "candidate_ref": inputs.get("candidate_ref"),
        "validation_run_id": inputs.get("validation_run_id"),
        "promotion_run_id": inputs.get("promotion_run_id"),
        "evidence_sha256": inputs.get("evidence_sha256"),
        "version": inputs.get("version"),
        "image_repository": IMAGE_REPOSITORY,
        "sha_tag": f"sha-{inputs.get('candidate_sha')}",
        "version_tag": f"v{inputs.get('version')}",
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        _fail("RECOVERY_RECEIPT_MISMATCH", "receipt scope differs from current dispatch")
    target = receipt.get("target_digest")
    if not isinstance(target, str) or not DIGEST_RE.fullmatch(target):
        _fail("IMAGE_DIGEST_INVALID", "recovery receipt has no valid target digest")
    return target


def verify_publication_recovery_registry(
    receipt: dict[str, Any], inputs: dict[str, Any], *, prior_run_id: int,
    prior_workflow_sha: str, runner: Runner = subprocess.run,
) -> dict[str, Any]:
    target = verify_publication_recovery_scope(
        receipt, inputs, prior_run_id=prior_run_id, prior_workflow_sha=prior_workflow_sha,
    )
    repository = receipt["image_repository"]
    for tag in (receipt["sha_tag"], receipt["version_tag"]):
        manifest = inspect_manifest(f"{repository}:{tag}", runner=runner)
        if manifest is None or manifest["digest"] != target:
            _fail("RECOVERY_TAG_MISMATCH", f"{tag} no longer resolves to receipt digest")
    index = inspect_manifest(f"{repository}@{target}", runner=runner)
    if index is None:
        _fail("RECOVERY_TAG_MISMATCH", "receipt digest is not reachable")
    platforms = {
        (item.get("platform", {}).get("os"), item.get("platform", {}).get("architecture"))
        for item in index.get("manifests", []) if isinstance(item, dict)
    }
    if platforms != {("linux", "amd64"), ("linux", "arm64")}:
        _fail("RECOVERY_PLATFORM_MISMATCH", "exact amd64/arm64 index required")
    return {"target_digest": target, "platforms": sorted(f"{os_}/{arch}" for os_, arch in platforms)}


Runner = Callable[..., subprocess.CompletedProcess[str]]


def inspect_manifest(reference: str, *, runner: Runner = subprocess.run) -> dict[str, Any] | None:
    completed = runner(
        ["docker", "buildx", "imagetools", "inspect", reference, "--format", "{{json .Manifest}}"],
        text=True, capture_output=True, check=False,
    )
    if completed.returncode != 0:
        combined = f"{completed.stdout}\n{completed.stderr}".lower()
        if any(token in combined for token in ("not found", "manifest unknown", "no such manifest")):
            return None
        _fail("REGISTRY_INSPECT_FAILED", reference)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MutationError("REGISTRY_INSPECT_FAILED", reference) from exc
    if not isinstance(payload, dict) or not DIGEST_RE.fullmatch(str(payload.get("digest", ""))):
        _fail("REGISTRY_INSPECT_FAILED", reference)
    return payload


def copy_manifest(source: str, target: str, *, runner: Runner = subprocess.run) -> None:
    completed = runner(
        ["docker", "buildx", "imagetools", "create", "--tag", target, source],
        text=True, capture_output=True, check=False,
    )
    if completed.returncode != 0:
        _fail("REGISTRY_COPY_FAILED", f"{source} -> {target}")


def attach_tag(
    *, repository: str, target_tag: str, source_digest: str, runner: Runner = subprocess.run,
) -> dict[str, Any]:
    if repository != IMAGE_REPOSITORY and not repository.startswith(("localhost:", "127.0.0.1:")):
        _fail("REGISTRY_SCOPE_REJECTED", repository)
    if not re.fullmatch(
        r"(?:candidate-[0-9a-f]{40}-[1-9][0-9]*-1|sha-[0-9a-f]{40}|"
        r"v[0-9]+\.[0-9]+\.[0-9]+|rollback-[1-9][0-9]*-1|latest)", target_tag,
    ):
        _fail("TAG_INVALID", target_tag)
    if not DIGEST_RE.fullmatch(source_digest):
        _fail("IMAGE_DIGEST_INVALID", source_digest)
    target = f"{repository}:{target_tag}"
    current = inspect_manifest(target, runner=runner)
    if current:
        if current["digest"] == source_digest:
            return {"tag": target_tag, "before": source_digest, "after": source_digest, "status": "noop"}
        _fail("TAG_CONFLICT", target)
    source = f"{repository}@{source_digest}"
    copy_manifest(source, target, runner=runner)
    observed = inspect_manifest(target, runner=runner)
    if not observed or observed["digest"] != source_digest:
        _fail("REGISTRY_VERIFY_FAILED", target)
    return {"tag": target_tag, "before": None, "after": source_digest, "status": "written"}


def move_latest(
    *, repository: str, target_digest: str, rollback_tag: str, runner: Runner = subprocess.run,
) -> list[dict[str, Any]]:
    latest = inspect_manifest(f"{repository}:latest", runner=runner)
    if not latest:
        _fail("PRIOR_LATEST_MISSING", repository)
    prior = latest["digest"]
    if prior == target_digest:
        _fail("ALREADY_DEPLOYED", target_digest)
    rollback = attach_tag(repository=repository, target_tag=rollback_tag, source_digest=prior, runner=runner)
    copy_manifest(f"{repository}@{target_digest}", f"{repository}:latest", runner=runner)
    observed = inspect_manifest(f"{repository}:latest", runner=runner)
    if not observed or observed["digest"] != target_digest:
        _fail("REGISTRY_VERIFY_FAILED", "latest")
    return [rollback, {"tag": "latest", "before": prior, "after": target_digest, "status": "written"}]


def replace_latest(
    *, repository: str, target_digest: str, expected_prior: str, runner: Runner = subprocess.run,
) -> dict[str, Any]:
    if repository != IMAGE_REPOSITORY and not repository.startswith(("localhost:", "127.0.0.1:")):
        _fail("REGISTRY_SCOPE_REJECTED", repository)
    if not DIGEST_RE.fullmatch(target_digest):
        _fail("IMAGE_DIGEST_INVALID", target_digest)
    if not DIGEST_RE.fullmatch(expected_prior):
        _fail("IMAGE_DIGEST_INVALID", expected_prior)
    latest = inspect_manifest(f"{repository}:latest", runner=runner)
    if not latest or latest["digest"] != expected_prior:
        _fail("LATEST_CHANGED", "latest changed after rollback capture")
    copy_manifest(f"{repository}@{target_digest}", f"{repository}:latest", runner=runner)
    observed = inspect_manifest(f"{repository}:latest", runner=runner)
    if not observed or observed["digest"] != target_digest:
        _fail("REGISTRY_VERIFY_FAILED", "latest")
    return {"tag": "latest", "before": expected_prior, "after": target_digest, "status": "written"}


def transition_state(state: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    phase = values.get("phase", state.get("phase"))
    if phase not in PHASES:
        _fail("STATE_INVALID", str(phase))
    old = state.get("phase", "preflight")
    if old not in PHASES or PHASES.index(phase) < PHASES.index(old):
        _fail("STATE_INVALID", f"phase regression {old}->{phase}")
    merged = dict(state)
    merged.update(values)
    merged["phase"] = phase
    merged["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return merged


def capture_failure(
    state: dict[str, Any], *, runner: Runner = subprocess.run,
    error_code: str | None = None, error_detail: str | None = None,
) -> dict[str, Any]:
    updated = dict(state)
    writes = [dict(item) for item in state.get("tag_writes", [])]
    repository = state.get("image_repository")
    for item in writes:
        if item.get("status") != "attempted" or not isinstance(repository, str):
            continue
        try:
            observed = inspect_manifest(f"{repository}:{item['tag']}", runner=runner)
        except MutationError:
            observed = None
        if observed and (item.get("after") is None or item.get("after") == observed["digest"]):
            item["after"] = observed["digest"]
            item["status"] = "written"
            if item["tag"] == state.get("staging_tag"):
                updated["target_digest"] = observed["digest"]
            if item["tag"] == "latest":
                updated["observed_latest_digest"] = observed["digest"]
        else:
            item["status"] = "failed"
    updated["tag_writes"] = writes
    attempted = writes[-1]["tag"] if writes else state.get("phase", "mutation")
    if state.get("kind") == "publication" and state.get("target_digest") and not state.get("platform_manifests"):
        code = "PLATFORM_PROBE_FAILED"
    elif state.get("kind") == "production" and state.get("phase") == "registry_verified":
        code = "PRODUCTION_OBSERVATION_FAILED"
    else:
        code = "MUTATION_STEP_FAILED"
    existing = state.get("error") if isinstance(state.get("error"), dict) else {}
    existing_code = str(existing.get("code", ""))
    if error_code is None and existing_code and not existing_code.endswith("_INCOMPLETE"):
        error_code = existing_code
        error_detail = str(existing.get("detail", "operation failed"))
    updated["status"] = "failed"
    updated["error"] = {
        "code": error_code or code,
        "detail": error_detail or f"operation failed at {attempted}",
    }
    return transition_state(updated, {"phase": updated["phase"]})


def validate_readiness(status_code: int, body: bytes, expected_version: str) -> str:
    if status_code in {401, 403}:
        return f"perimeter_{status_code}"
    if status_code != 200:
        _fail("PRODUCTION_NOT_READY", f"HTTP {status_code}")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MutationError("PRODUCTION_NOT_READY", "readiness body is not JSON") from exc
    if payload.get("ready") is not True or payload.get("version") != expected_version:
        _fail("PRODUCTION_NOT_READY", "ready/version mismatch")
    return "ready_true"


def render_receipt(state: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    kind = state.get("kind")
    if kind not in {"publication", "production"}:
        _fail("RECEIPT_INVALID", "unknown kind")
    validate_receipt(state)
    if output_dir.exists():
        _fail("RECEIPT_INVALID", "output already exists")
    temporary = Path(tempfile.mkdtemp(prefix=".receipt-", dir=output_dir.parent))
    try:
        raw = canonical_json(state)
        digest = hashlib.sha256(raw).hexdigest()
        (temporary / "receipt.json").write_bytes(raw)
        (temporary / "receipt.sha256").write_text(digest + "\n", encoding="ascii")
        error = state.get("error") or {}
        rollback = ""
        if kind == "production":
            rollback = f"<h2>Rollback</h2><dl><dt>Prior digest</dt><dd><code>{html.escape(state['prior_latest_digest'])}</code></dd><dt>Rollback tag</dt><dd><code>{html.escape(state['rollback_tag'])}</code></dd><dt>Restore command</dt><dd><code>{html.escape(state['rollback_command'])}</code></dd><dt>Readiness</dt><dd>{html.escape(state['public_health']['ready_observation'])}</dd></dl>"
        tools = " · ".join(f"{html.escape(name)} {html.escape(value)}" for name, value in state["tool_versions"].items())
        page = f"""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>ODIN {html.escape(kind.title())} Receipt</title><style>body{{font:16px/1.5 system-ui;max-width:960px;margin:auto;padding:32px;color:#16202a}}code{{overflow-wrap:anywhere}}dt{{font-weight:700}}.ok{{color:#08783e}}.bad{{color:#b42318}}</style></head><body><main><h1>ODIN {html.escape(kind.title())} Receipt</h1><p class=\"{'ok' if state['status']=='success' else 'bad'}\" role=\"status\">{html.escape(state['status'].upper())}</p><dl><dt>Phase</dt><dd>{html.escape(state['phase'])}</dd><dt>Candidate</dt><dd><code>{html.escape(state['candidate_sha'])}</code></dd><dt>Version</dt><dd>{html.escape(state['version'])}</dd><dt>Target digest</dt><dd><code>{html.escape(str(state.get('target_digest')))}</code></dd><dt>Error</dt><dd>{html.escape(str(error.get('code', 'none')))}</dd><dt>Tools</dt><dd>{tools}</dd></dl>{rollback}<p>Receipt SHA-256: <code>{digest}</code></p></main></body></html>"""
        (temporary / "index.html").write_text(page, encoding="utf-8")
        os.replace(temporary, output_dir)
        return {"receipt_sha256": digest, "output_dir": str(output_dir)}
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _command() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-dispatch")
    validate.add_argument("--kind", choices=("publication", "production"), required=True)
    validate.add_argument("--inputs", type=Path, required=True)
    validate.add_argument("--current", type=Path, required=True)
    select = commands.add_parser("select-artifact")
    select.add_argument("--workflow-run", type=Path, required=True)
    select.add_argument("--artifacts", type=Path, required=True)
    select.add_argument("--run-id", type=int, required=True)
    select.add_argument("--workflow-path", required=True)
    select.add_argument("--artifact-name", required=True)
    select.add_argument("--expected-conclusion", choices=("success", "failure"), default="success")
    select.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-promotion")
    verify.add_argument("--artifact-zip", type=Path, required=True)
    verify.add_argument("--output-dir", type=Path, required=True)
    verify.add_argument("--evidence-dir", type=Path, required=True)
    verify.add_argument("--action", choices=("stage", "production"), required=True)
    verify.add_argument("--candidate-sha", required=True)
    verify.add_argument("--evidence-sha256", required=True)
    verify.add_argument("--promotion-run-id", type=int, required=True)
    receipt = commands.add_parser("verify-receipt")
    receipt.add_argument("--artifact-zip", type=Path, required=True)
    receipt.add_argument("--output-dir", type=Path, required=True)
    receipt.add_argument("--kind", choices=("publication", "production"), required=True)
    receipt.add_argument("--sha256", required=True)
    recovery = commands.add_parser("verify-publication-recovery")
    recovery.add_argument("--receipt", type=Path, required=True)
    recovery.add_argument("--inputs", type=Path, required=True)
    recovery.add_argument("--prior-run-id", type=int, required=True)
    recovery.add_argument("--prior-workflow-sha", required=True)
    attach = commands.add_parser("attach-tag")
    attach.add_argument("--repository", required=True)
    attach.add_argument("--tag", required=True)
    attach.add_argument("--digest", required=True)
    move = commands.add_parser("move-latest")
    move.add_argument("--repository", required=True)
    move.add_argument("--digest", required=True)
    move.add_argument("--rollback-tag", required=True)
    replace = commands.add_parser("replace-latest")
    replace.add_argument("--repository", required=True)
    replace.add_argument("--digest", required=True)
    replace.add_argument("--expected-prior", required=True)
    transition = commands.add_parser("transition-state")
    transition.add_argument("--state", type=Path, required=True)
    transition.add_argument("--values", type=Path, required=True)
    render = commands.add_parser("render-receipt")
    render.add_argument("--state", type=Path, required=True)
    render.add_argument("--output-dir", type=Path, required=True)
    failure = commands.add_parser("capture-failure")
    failure.add_argument("--state", type=Path, required=True)
    failure.add_argument("--error-code")
    failure.add_argument("--error-detail")
    ready = commands.add_parser("verify-readiness")
    ready.add_argument("--status-code", type=int, required=True)
    ready.add_argument("--body", type=Path, required=True)
    ready.add_argument("--version", required=True)
    args = parser.parse_args()
    if args.command == "validate-dispatch":
        validate_dispatch(_read_json(args.inputs), _read_json(args.current), kind=args.kind)
    elif args.command == "select-artifact":
        observation, artifact_id = select_artifact(
            _read_json(args.workflow_run), _read_json(args.artifacts), run_id=args.run_id,
            workflow_path=args.workflow_path, artifact_name=args.artifact_name,
            expected_conclusion=args.expected_conclusion,
        )
        _atomic_json(args.output, observation); print(artifact_id)
    elif args.command == "verify-promotion":
        result = verify_promotion_artifact(
            args.artifact_zip, args.output_dir, args.evidence_dir, expected_action=args.action,
            expected_candidate=args.candidate_sha, expected_evidence=args.evidence_sha256,
            expected_run_id=args.promotion_run_id,
        ); print(json.dumps(result, sort_keys=True))
    elif args.command == "verify-receipt":
        print(json.dumps(verify_receipt_bundle(
            args.artifact_zip, args.output_dir, expected_kind=args.kind, expected_sha256=args.sha256,
        ), sort_keys=True))
    elif args.command == "verify-publication-recovery":
        print(json.dumps(verify_publication_recovery_registry(
            _read_json(args.receipt), _read_json(args.inputs), prior_run_id=args.prior_run_id,
            prior_workflow_sha=args.prior_workflow_sha,
        ), sort_keys=True))
    elif args.command == "attach-tag":
        print(json.dumps(attach_tag(repository=args.repository, target_tag=args.tag, source_digest=args.digest), sort_keys=True))
    elif args.command == "move-latest":
        print(json.dumps(move_latest(repository=args.repository, target_digest=args.digest, rollback_tag=args.rollback_tag), sort_keys=True))
    elif args.command == "replace-latest":
        print(json.dumps(replace_latest(
            repository=args.repository, target_digest=args.digest, expected_prior=args.expected_prior,
        ), sort_keys=True))
    elif args.command == "transition-state":
        _atomic_json(args.state, transition_state(_read_json(args.state), _read_json(args.values)))
    elif args.command == "capture-failure":
        _atomic_json(args.state, capture_failure(
            _read_json(args.state), error_code=args.error_code, error_detail=args.error_detail,
        ))
    elif args.command == "verify-readiness":
        print(validate_readiness(args.status_code, args.body.read_bytes(), args.version))
    else:
        print(json.dumps(render_receipt(_read_json(args.state), args.output_dir), sort_keys=True))
    return 0


def main() -> int:
    try:
        return _command()
    except MutationError as exc:
        print(json.dumps({"status": "fail", "code": exc.code, "detail": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
