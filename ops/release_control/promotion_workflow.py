"""Offline helpers for the authenticated promotion-eligibility workflow."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .practical_evidence import MAX_FILE_BYTES, canonical_json, verify_bundle
from .promotion_eligibility import EligibilityError, normalize_validation_observation, verify_eligibility
from .release_authorization import text_sha256

OWNER_LOGIN = "HughKantsime"
OWNER_ID = 201174638
WORKFLOW_PATH = ".github/workflows/promote.yml"
VALIDATION_PATH = ".github/workflows/trusted-validation.yml"
EVIDENCE_FILES = frozenset({"manifest.json", "manifest.sha256", "index.html"})
FETCH_ALLOWLIST = (
    "ops/__init__.py",
    "ops/release_control/__init__.py",
    "ops/edu_readiness/__init__.py",
    "ops/release_control/promotion_workflow.py",
    "ops/release_control/promotion_eligibility.py",
    "ops/release_control/practical_evidence.py",
    "ops/release_control/release_authorization.py",
    "ops/release_control/policy.py",
    "ops/release_control/run_gate.py",
    "ops/edu_readiness/artifact_scan.py",
    "ops/edu_readiness/common.py",
    "ops/release_control/practical_evidence.schema.json",
    "ops/release_control/release_authorization.schema.json",
    "ops/release_control/validation_run_observation.schema.json",
    "ops/release_control/promotion_context.schema.json",
    "ops/release_control/result.schema.json",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class PromotionWorkflowError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise PromotionWorkflowError(code, detail)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionWorkflowError("OBSERVATION_INVALID", str(path)) from exc


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _validate_schema(payload: dict[str, Any], name: str, code: str) -> None:
    schema = json.loads(Path(__file__).with_name(name).read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda item: list(item.path),
    )
    if errors:
        _fail(code, errors[0].message)


def validate_inputs(inputs: dict[str, Any], current: dict[str, Any]) -> None:
    if current.get("actor_login") != OWNER_LOGIN or current.get("actor_id") != OWNER_ID:
        _fail("ACTOR_MISMATCH", "promotion actor is not the exact owner")
    if current.get("triggering_actor") != OWNER_LOGIN:
        _fail("TRIGGERING_ACTOR_MISMATCH", "triggering actor is not the exact owner")
    if current.get("run_attempt") != 1:
        _fail("RUN_ATTEMPT_REJECTED", "promotion reruns are not eligible")
    action = inputs.get("action")
    if action not in {"stage", "demo", "production"}:
        _fail("AUTHORIZATION_SCOPE_MISMATCH", "unknown action")
    run_id = inputs.get("validation_run_id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        _fail("OBSERVATION_INVALID", "validation run ID must be positive")
    candidate_sha = inputs.get("candidate_sha")
    candidate_ref = inputs.get("candidate_ref")
    if not isinstance(candidate_sha, str) or not SHA_RE.fullmatch(candidate_sha) \
            or candidate_ref != f"release-candidate/{candidate_sha}":
        _fail("CANDIDATE_MISMATCH", "candidate SHA/ref mismatch")
    digest = inputs.get("evidence_sha256")
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        _fail("EVIDENCE_DIGEST_MISMATCH", "invalid evidence digest")
    nonce = inputs.get("nonce")
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        _fail("AUTHORIZATION_INVALID", "invalid nonce")
    text = inputs.get("authorization_text")
    if not isinstance(text, str) or not 8 <= len(text) <= 1000:
        _fail("AUTHORIZATION_INVALID", "authorization text must contain 8-1000 characters")
    expected_current = {
        "repository_id": int,
        "repository": str,
        "run_id": int,
        "workflow_sha": str,
        "workflow_ref": str,
    }
    if any(not isinstance(current.get(key), expected) for key, expected in expected_current.items()) \
            or not SHA_RE.fullmatch(current["workflow_sha"]) \
            or current.get("event") != "workflow_dispatch" \
            or current.get("workflow_path") != WORKFLOW_PATH:
        _fail("WORKFLOW_IDENTITY_MISMATCH", "invalid current workflow context")


def select_observation(
    workflow_run: dict[str, Any], artifact_payload: dict[str, Any], validation_run_id: int,
) -> tuple[dict[str, Any], int]:
    if workflow_run.get("id") != validation_run_id:
        _fail("WORKFLOW_IDENTITY_MISMATCH", "queried validation run ID mismatch")
    expected_name = f"odin-trusted-validation-{validation_run_id}-1"
    artifacts = artifact_payload.get("artifacts")
    if not isinstance(artifacts, list):
        _fail("OBSERVATION_INVALID", "artifact response is not an object list")
    if artifact_payload.get("total_count") != 1 or len(artifacts) != 1:
        _fail("ARTIFACT_MISMATCH", "artifact query did not return exactly one result")
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("name") == expected_name]
    if len(matches) != 1:
        _fail("ARTIFACT_MISMATCH", f"expected exactly one {expected_name} artifact")
    artifact = matches[0]
    if workflow_run.get("run_attempt") != 1:
        _fail("RUN_ATTEMPT_REJECTED", "validation reruns are not eligible")
    if workflow_run.get("event") != "workflow_dispatch":
        _fail("EVENT_REJECTED", "validation was not manually dispatched")
    if workflow_run.get("conclusion") != "success":
        _fail("VALIDATION_NOT_SUCCESSFUL", str(workflow_run.get("conclusion")))
    actor = workflow_run.get("actor", {})
    if actor.get("login") != OWNER_LOGIN or actor.get("id") != OWNER_ID:
        _fail("ACTOR_MISMATCH", "validation actor is not the exact owner")
    if workflow_run.get("triggering_actor", {}).get("login") != OWNER_LOGIN:
        _fail("TRIGGERING_ACTOR_MISMATCH", "validation triggering actor is not the exact owner")
    if workflow_run.get("path") != VALIDATION_PATH or workflow_run.get("head_branch") != "main":
        _fail("WORKFLOW_IDENTITY_MISMATCH", "validation workflow identity mismatch")
    artifact_run = artifact.get("workflow_run", {})
    repository = workflow_run.get("repository", {})
    if (artifact_run.get("id") != validation_run_id
            or artifact_run.get("repository_id") != repository.get("id")
            or artifact_run.get("head_repository_id") != repository.get("id")
            or artifact_run.get("head_sha") != workflow_run.get("head_sha")):
        _fail("ARTIFACT_MISMATCH", "artifact is not linked to the exact validation run")
    if artifact.get("expired") is not False or not isinstance(artifact.get("id"), int):
        _fail("ARTIFACT_MISMATCH", "artifact is expired or lacks an ID")
    try:
        normalized = normalize_validation_observation({"workflow_run": workflow_run, "artifact": artifact})
    except EligibilityError as exc:
        raise PromotionWorkflowError(exc.code, str(exc)) from exc
    return normalized, artifact["id"]


def normalize_environment_review(payload: Any, action: str, run_id: int) -> dict[str, Any] | None:
    if action != "production":
        return None
    if not isinstance(payload, list):
        _fail("PRODUCTION_APPROVAL_MISSING", "approval response is not a list")
    matches = []
    for review in payload:
        environments = review.get("environments", []) if isinstance(review, dict) else []
        names = {item.get("name") for item in environments if isinstance(item, dict)}
        user = review.get("user", {}) if isinstance(review, dict) else {}
        if (review.get("state") == "approved" and user.get("login") == OWNER_LOGIN
                and user.get("id") == OWNER_ID and "production" in names):
            matches.append(review)
    if len(matches) != 1:
        _fail("PRODUCTION_APPROVAL_MISSING", "exact owner production approval is missing")
    return {"state": "approved", "actor_login": OWNER_LOGIN, "actor_id": OWNER_ID, "run_id": run_id}


def build_records(
    inputs: dict[str, Any], current: dict[str, Any], validation: dict[str, Any],
    approval_payload: Any, *, now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_inputs(inputs, current)
    issued = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    expires = issued + timedelta(hours=1 if inputs["action"] == "production" else 24)
    text_digest = text_sha256(inputs["authorization_text"])
    context = {
        "schema_version": 1,
        "repository_id": current["repository_id"],
        "repository": current["repository"],
        "run_id": current["run_id"],
        "run_attempt": current["run_attempt"],
        "actor_login": current["actor_login"],
        "actor_id": current["actor_id"],
        "triggering_actor": current["triggering_actor"],
        "event": current["event"],
        "workflow_path": current["workflow_path"],
        "workflow_ref": current["workflow_ref"],
        "workflow_sha": current["workflow_sha"],
        "action": inputs["action"],
        "environment": inputs["action"],
        "evidence_sha256": inputs["evidence_sha256"],
        "nonce": inputs["nonce"],
        "authorization_text_sha256": text_digest,
        "environment_review": normalize_environment_review(
            approval_payload, inputs["action"], current["run_id"]
        ),
    }
    request = {
        "schema_version": 1,
        "action": inputs["action"],
        "repository_id": current["repository_id"],
        "repository": current["repository"],
        "candidate_sha": inputs["candidate_sha"],
        "candidate_ref": inputs["candidate_ref"],
        "evidence_sha256": inputs["evidence_sha256"],
        "validation_run_id": validation["run_id"],
        "validation_run_attempt": validation["run_attempt"],
        "validation_workflow_sha": validation["head_sha"],
        "validation_artifact_name": validation["artifact"]["name"],
        "validation_artifact_digest": validation["artifact"]["digest"],
        "promotion_run_id": current["run_id"],
        "promotion_run_attempt": current["run_attempt"],
        "promotion_workflow_sha": current["workflow_sha"],
        "actor_login": current["actor_login"],
        "actor_id": current["actor_id"],
        "triggering_actor": current["triggering_actor"],
        "environment": inputs["action"],
        "nonce": inputs["nonce"],
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "authorization_text": inputs["authorization_text"],
        "authorization_text_sha256": text_digest,
    }
    _validate_schema(request, "release_authorization.schema.json", "AUTHORIZATION_INVALID")
    _validate_schema(context, "promotion_context.schema.json", "OBSERVATION_INVALID")
    return request, context


def _safe_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PromotionWorkflowError("EVIDENCE_NODE_FORBIDDEN", str(path)) from exc
    if not stat.S_ISREG(metadata.st_mode):
        _fail("EVIDENCE_NODE_FORBIDDEN", str(path))
    if metadata.st_size > MAX_FILE_BYTES:
        _fail("EVIDENCE_FILE_TOO_LARGE", str(path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PromotionWorkflowError("EVIDENCE_NODE_FORBIDDEN", str(path)) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
            _fail("EVIDENCE_NODE_FORBIDDEN", str(path))
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def verify_download(evidence_dir: Path, expected_sha256: str) -> dict[str, Any]:
    try:
        nodes = list(evidence_dir.iterdir())
    except OSError as exc:
        raise PromotionWorkflowError("EVIDENCE_INVALID", str(evidence_dir)) from exc
    names = {item.name for item in nodes}
    if names != EVIDENCE_FILES or len(nodes) != len(EVIDENCE_FILES):
        _fail("EVIDENCE_INVALID", "download must contain exactly the three evidence files")
    for node in nodes:
        _safe_file(node)
    try:
        verified = verify_bundle(evidence_dir)
    except Exception as exc:
        code = getattr(exc, "code", "EVIDENCE_INVALID")
        raise PromotionWorkflowError(code, str(exc)) from exc
    if verified["evidence_sha256"] != expected_sha256:
        _fail("EVIDENCE_DIGEST_MISMATCH", "downloaded manifest does not match authorized digest")
    return verified


def extract_and_verify(zip_path: Path, evidence_dir: Path, expected_sha256: str) -> dict[str, Any]:
    if evidence_dir.exists() or evidence_dir.is_symlink():
        _fail("EVIDENCE_INVALID", "evidence output already exists")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            names = {item.filename for item in members}
            if names != EVIDENCE_FILES or len(members) != len(EVIDENCE_FILES):
                _fail("EVIDENCE_INVALID", "artifact ZIP must contain exact root files")
            for item in members:
                mode = item.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if item.file_size > MAX_FILE_BYTES:
                    _fail("EVIDENCE_FILE_TOO_LARGE", item.filename)
                if "/" in item.filename or "\\" in item.filename or item.is_dir() \
                        or file_type not in (0, stat.S_IFREG):
                    _fail("EVIDENCE_NODE_FORBIDDEN", item.filename)
            temporary = Path(tempfile.mkdtemp(prefix=".evidence-", dir=evidence_dir.parent))
            try:
                for item in members:
                    (temporary / item.filename).write_bytes(archive.read(item))
                os.replace(temporary, evidence_dir)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise PromotionWorkflowError("EVIDENCE_INVALID", str(zip_path)) from exc
    try:
        return verify_download(evidence_dir, expected_sha256)
    except BaseException:
        shutil.rmtree(evidence_dir, ignore_errors=True)
        raise


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(data)).encode("ascii") + b"\0" + data,
        usedforsecurity=False,
    ).hexdigest()


def fetch_files(responses: list[dict[str, Any]], output_root: Path) -> None:
    if output_root.exists() or output_root.is_symlink():
        _fail("FETCH_OUTPUT_EXISTS", str(output_root))
    if len(responses) != len(FETCH_ALLOWLIST):
        _fail("FETCH_SET_MISMATCH", "response count does not match exact allowlist")
    decoded: dict[str, bytes] = {}
    for response in responses:
        if not isinstance(response, dict):
            _fail("FETCH_RESPONSE_INVALID", "response is not an object")
        path = response.get("path")
        if path not in FETCH_ALLOWLIST or path in decoded or response.get("type") != "file" \
                or response.get("encoding") != "base64":
            _fail("FETCH_SET_MISMATCH", str(path))
        try:
            encoded = "".join(response["content"].split())
            data = base64.b64decode(encoded, validate=True)
        except (AttributeError, KeyError, ValueError) as exc:
            raise PromotionWorkflowError("FETCH_RESPONSE_INVALID", str(path)) from exc
        if response.get("size") != len(data) or not isinstance(response.get("sha"), str) \
                or not SHA_RE.fullmatch(response["sha"]) or _git_blob_sha(data) != response["sha"]:
            _fail("FETCH_INTEGRITY_MISMATCH", str(path))
        decoded[path] = data
    if set(decoded) != set(FETCH_ALLOWLIST):
        _fail("FETCH_SET_MISMATCH", "response paths do not match exact allowlist")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".promotion-bundle-", dir=output_root.parent))
    try:
        for relative in FETCH_ALLOWLIST:
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(decoded[relative])
        os.replace(temporary, output_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def render_decision(
    output_dir: Path, decision: dict[str, Any], request: dict[str, Any],
    validation: dict[str, Any], context: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("request.json", request),
        ("validation-observation.json", validation),
        ("promotion-context.json", context),
        ("decision.json", decision),
    ):
        _atomic_json(output_dir / name, payload)
    status = html.escape(str(decision.get("status", "fail")).upper())
    action = html.escape(str(request.get("action", "unknown")))
    candidate = html.escape(str(request.get("candidate_sha", "unknown")))
    digest = html.escape(str(request.get("evidence_sha256", "unknown")))
    code = html.escape(str(decision.get("code", "")))
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ODIN Promotion Eligibility</title><style>body{{font:16px/1.5 system-ui;max-width:900px;margin:auto;padding:32px;color:#16202a}}code{{overflow-wrap:anywhere}}.status{{font-weight:800}}</style></head><body><main><h1>ODIN Promotion Eligibility</h1><p class="status" role="status">{status}</p><dl><dt>Action</dt><dd>{action}</dd><dt>Candidate</dt><dd><code>{candidate}</code></dd><dt>Evidence SHA-256</dt><dd><code>{digest}</code></dd><dt>Decision code</dt><dd>{code or "eligible"}</dd></dl></main></body></html>"""
    temporary = output_dir / ".index.html.tmp"
    temporary.write_text(page, encoding="utf-8")
    os.replace(temporary, output_dir / "index.html")


def _command() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-inputs")
    validate.add_argument("--inputs", required=True, type=Path)
    validate.add_argument("--current-context", required=True, type=Path)
    select = commands.add_parser("select-observation")
    select.add_argument("--workflow-run", required=True, type=Path)
    select.add_argument("--artifacts", required=True, type=Path)
    select.add_argument("--validation-run-id", required=True, type=int)
    select.add_argument("--output", required=True, type=Path)
    download = commands.add_parser("verify-download")
    download.add_argument("--artifact-zip", type=Path)
    download.add_argument("--evidence-dir", required=True, type=Path)
    download.add_argument("--expected-sha256", required=True)
    build = commands.add_parser("build-records")
    build.add_argument("--inputs", required=True, type=Path)
    build.add_argument("--current-context", required=True, type=Path)
    build.add_argument("--validation-observation", required=True, type=Path)
    build.add_argument("--approvals", required=True, type=Path)
    build.add_argument("--request-out", required=True, type=Path)
    build.add_argument("--context-out", required=True, type=Path)
    evaluate = commands.add_parser("render-decision")
    evaluate.add_argument("--evidence-dir", required=True, type=Path)
    evaluate.add_argument("--request", required=True, type=Path)
    evaluate.add_argument("--validation-observation", required=True, type=Path)
    evaluate.add_argument("--promotion-context", required=True, type=Path)
    evaluate.add_argument("--output-dir", required=True, type=Path)
    fetch = commands.add_parser("fetch-files")
    fetch.add_argument("--responses", required=True, type=Path)
    fetch.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "validate-inputs":
        validate_inputs(_read_json(args.inputs), _read_json(args.current_context))
    elif args.command == "select-observation":
        observation, artifact_id = select_observation(
            _read_json(args.workflow_run), _read_json(args.artifacts), args.validation_run_id
        )
        _atomic_json(args.output, observation)
        print(artifact_id)
    elif args.command == "verify-download":
        if args.artifact_zip:
            extract_and_verify(args.artifact_zip, args.evidence_dir, args.expected_sha256)
        else:
            verify_download(args.evidence_dir, args.expected_sha256)
    elif args.command == "build-records":
        request, context = build_records(
            _read_json(args.inputs), _read_json(args.current_context),
            _read_json(args.validation_observation), _read_json(args.approvals),
        )
        _atomic_json(args.request_out, request)
        _atomic_json(args.context_out, context)
    elif args.command == "render-decision":
        request = _read_json(args.request)
        validation = _read_json(args.validation_observation)
        context = _read_json(args.promotion_context)
        try:
            decision = verify_eligibility(
                args.evidence_dir, args.request, args.validation_observation, args.promotion_context
            )
        except EligibilityError as exc:
            decision = {"status": "fail", "code": exc.code, "detail": str(exc)}
            render_decision(args.output_dir, decision, request, validation, context)
            raise PromotionWorkflowError(exc.code, str(exc)) from exc
        render_decision(args.output_dir, decision, request, validation, context)
    else:
        payload = _read_json(args.responses)
        if not isinstance(payload, list):
            _fail("FETCH_RESPONSE_INVALID", "responses must be a list")
        fetch_files(payload, args.output_root)
    return 0


def main() -> int:
    try:
        return _command()
    except PromotionWorkflowError as exc:
        print(json.dumps({"status": "fail", "code": exc.code, "detail": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
