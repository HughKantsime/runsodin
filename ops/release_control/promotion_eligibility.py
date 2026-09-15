"""Read-only matching of evidence to trusted validation and promotion observations."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .practical_evidence import EvidenceError, verify_bundle
from .release_authorization import AuthorizationError, load_and_validate

OWNER_LOGIN = "HughKantsime"
OWNER_ID = 201174638


class EligibilityError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def _load(path: Path, schema_name: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EligibilityError("OBSERVATION_INVALID", str(path)) from exc
    schema = json.loads(Path(__file__).with_name(schema_name).read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
                    key=lambda item: list(item.path))
    if errors:
        raise EligibilityError("OBSERVATION_INVALID", errors[0].message)
    return payload


def normalize_validation_observation(payload: dict[str, Any]) -> dict[str, Any]:
    """Select the fields supplied by GitHub's workflow-run and artifact APIs."""
    try:
        run = payload["workflow_run"]
        artifact = payload["artifact"]
        artifact_run = artifact["workflow_run"]
        if (artifact_run["id"] != run["id"] or artifact_run["repository_id"] != run["repository"]["id"]
                or artifact_run["head_sha"] != run["head_sha"]):
            raise EligibilityError("ARTIFACT_MISMATCH", "artifact is not attached to observed validation run")
        normalized = {
            "schema_version": 1,
            "repository_id": run["repository"]["id"],
            "repository": run["repository"]["full_name"],
            "run_id": run["id"],
            "run_attempt": run["run_attempt"],
            "actor_login": run["actor"]["login"],
            "actor_id": run["actor"]["id"],
            "triggering_actor": run["triggering_actor"]["login"],
            "event": run["event"],
            "conclusion": run["conclusion"],
            "path": run["path"],
            "head_sha": run["head_sha"],
            "head_branch": run["head_branch"],
            "artifact": {"name": artifact["name"], "digest": artifact["digest"]},
        }
    except (KeyError, TypeError) as exc:
        raise EligibilityError("OBSERVATION_INVALID", "invalid GitHub API observation") from exc
    schema = json.loads(Path(__file__).with_name("validation_run_observation.schema.json").read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(normalized), key=lambda item: list(item.path))
    if errors:
        raise EligibilityError("OBSERVATION_INVALID", errors[0].message)
    return normalized


def _load_validation(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EligibilityError("OBSERVATION_INVALID", str(path)) from exc
    if not isinstance(payload, dict):
        raise EligibilityError("OBSERVATION_INVALID", str(path))
    if payload.get("schema_version") == 1:
        schema = json.loads(Path(__file__).with_name("validation_run_observation.schema.json").read_text(encoding="utf-8"))
        errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path))
        if errors:
            raise EligibilityError("OBSERVATION_INVALID", errors[0].message)
        return payload
    return normalize_validation_observation(payload)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EligibilityError("AUTHORIZATION_TIME_INVALID", value)
    return parsed.astimezone(timezone.utc)


def verify_eligibility(
    evidence_dir: Path, request_path: Path, validation_path: Path, promotion_path: Path,
    *, now: datetime | None = None,
) -> dict[str, Any]:
    try:
        evidence = verify_bundle(evidence_dir)
        request = load_and_validate(request_path)
    except EvidenceError as exc:
        raise EligibilityError(exc.code, str(exc)) from exc
    except AuthorizationError as exc:
        raise EligibilityError(exc.code, str(exc)) from exc
    validation = _load_validation(validation_path)
    promotion = _load(promotion_path, "promotion_context.schema.json")
    manifest = evidence["manifest"]

    if request["actor_login"] != OWNER_LOGIN or request["actor_id"] != OWNER_ID \
            or validation["actor_login"] != OWNER_LOGIN or validation["actor_id"] != OWNER_ID \
            or promotion["actor_login"] != OWNER_LOGIN or promotion["actor_id"] != OWNER_ID:
        raise EligibilityError("ACTOR_MISMATCH", "owner identity is not exact")
    if request["triggering_actor"] != OWNER_LOGIN or validation["triggering_actor"] != OWNER_LOGIN \
            or promotion["triggering_actor"] != OWNER_LOGIN:
        raise EligibilityError("TRIGGERING_ACTOR_MISMATCH", "triggering actor is not exact")
    if validation["run_attempt"] != 1 or promotion["run_attempt"] != 1 \
            or request["validation_run_attempt"] != 1 or request["promotion_run_attempt"] != 1:
        raise EligibilityError("RUN_ATTEMPT_REJECTED", "reruns are not eligible")
    if validation["event"] != "workflow_dispatch" or promotion["event"] != "workflow_dispatch":
        raise EligibilityError("EVENT_REJECTED", "both runs must be manually dispatched")
    if validation["conclusion"] != "success":
        raise EligibilityError("VALIDATION_NOT_SUCCESSFUL", validation["conclusion"])
    if request["candidate_sha"] != manifest["source_commit"] or request["candidate_ref"] != manifest["candidate_ref"]:
        raise EligibilityError("CANDIDATE_MISMATCH", "request does not match evidence source")
    if request["evidence_sha256"] != evidence["evidence_sha256"] or promotion["evidence_sha256"] != evidence["evidence_sha256"]:
        raise EligibilityError("EVIDENCE_DIGEST_MISMATCH", "promotion did not hash these evidence bytes")
    if validation["path"] != ".github/workflows/trusted-validation.yml" \
            or validation["head_branch"] != "main" or request["validation_workflow_sha"] != validation["head_sha"]:
        raise EligibilityError("WORKFLOW_IDENTITY_MISMATCH", "validation workflow identity mismatch")
    if request["validation_artifact_name"] != validation["artifact"]["name"] \
            or request["validation_artifact_digest"] != validation["artifact"]["digest"]:
        raise EligibilityError("ARTIFACT_MISMATCH", "validation artifact mismatch")
    if request["repository_id"] != validation["repository_id"] or request["repository_id"] != promotion["repository_id"] \
            or request["repository"] != validation["repository"] or request["repository"] != promotion["repository"] \
            or request["validation_run_id"] != validation["run_id"] \
            or request["promotion_run_id"] != promotion["run_id"] or validation["run_id"] == promotion["run_id"]:
        raise EligibilityError("WORKFLOW_IDENTITY_MISMATCH", "run/repository identity mismatch")
    if manifest["run_id"] != f"gha-{validation['run_id']}-{validation['run_attempt']}":
        raise EligibilityError("WORKFLOW_IDENTITY_MISMATCH", "evidence run ID is not bound to validation run")
    if promotion["workflow_path"] != ".github/workflows/promote.yml" \
            or not promotion["workflow_ref"].endswith("/.github/workflows/promote.yml@refs/heads/main") \
            or request["promotion_workflow_sha"] != promotion["workflow_sha"]:
        raise EligibilityError("WORKFLOW_IDENTITY_MISMATCH", "promotion workflow identity mismatch")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued, expires = _time(request["issued_at"]), _time(request["expires_at"])
    maximum = 3600 if request["action"] == "production" else 86400
    if issued > current or current >= expires or (expires - issued).total_seconds() > maximum:
        raise EligibilityError("AUTHORIZATION_TIME_INVALID", "request is future, expired, or too long-lived")
    if request["action"] != request["environment"] or request["action"] != promotion["action"] \
            or request["environment"] != promotion["environment"]:
        raise EligibilityError("AUTHORIZATION_SCOPE_MISMATCH", "action/environment mismatch")
    if request["nonce"] != promotion["nonce"] \
            or request["authorization_text_sha256"] != promotion["authorization_text_sha256"]:
        raise EligibilityError("AUTHORIZATION_TEXT_MISMATCH", "trusted promotion inputs do not match request")
    if request["action"] == "production":
        review = promotion["environment_review"]
        if (promotion["environment"] != "production" or not isinstance(review, dict)
                or review.get("state") != "approved" or review.get("actor_login") != OWNER_LOGIN
                or review.get("actor_id") != OWNER_ID or review.get("run_id") != promotion["run_id"]):
            raise EligibilityError("PRODUCTION_APPROVAL_MISSING", "promotion run lacks exact approval")
    return {
        "status": "eligible", "action": request["action"], "candidate_sha": manifest["source_commit"],
        "evidence_sha256": evidence["evidence_sha256"], "validation_run_id": validation["run_id"],
        "promotion_run_id": promotion["run_id"], "nonce": request["nonce"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--promotion-request", required=True, type=Path)
    parser.add_argument("--validation-run-observation", required=True, type=Path)
    parser.add_argument("--promotion-context", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify_eligibility(
            args.evidence_dir, args.promotion_request, args.validation_run_observation,
            args.promotion_context,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except EligibilityError as exc:
        print(json.dumps({"status": "fail", "code": exc.code, "detail": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
