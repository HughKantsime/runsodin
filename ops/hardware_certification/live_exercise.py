"""One-time-authorized live exercise orchestration and sanitized evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .active import ActiveBackend, canonical_remote_name, execute_authorized
from .active_bambu import BambuLiveSession
from .active_elegoo import ElegooLiveSession
from .active_http import MoonrakerLiveSession, PrusaLinkLiveSession
from .artifact import ROOT, git_identity, publish_result, utc_iso
from .assets import validate_test_asset
from .authorization import AuthorizationConsumedError
from .config import ResolvedTarget, validate_target_shape
from .security import (
    SensitiveValueRegistry, canonical_json_sha256, resolve_private_target,
    target_correlation_sha256,
)


ASSET_ACTIONS = {"upload", "start", "upload_start"}


def authorization_scope(authorization: dict, target: ResolvedTarget | dict) -> dict:
    model_family = target.model_family if isinstance(target, ResolvedTarget) else target["model_family"]
    correlation_key = (
        target.evidence_correlation_key
        if isinstance(target, ResolvedTarget)
        else target["evidence_correlation_key"]
    )
    return {
        "schema_version": authorization["schema_version"],
        "run_id": authorization["run_id"],
        "protocol": authorization["protocol"],
        "actions": [item["name"] for item in authorization["actions"]],
        "expires_at": authorization["expires_at"],
        "model_family": model_family,
        "target_correlation_sha256": target_correlation_sha256(correlation_key),
    }


def authorization_scope_sha256(scope: dict) -> str:
    return canonical_json_sha256(scope)


def _resolved(target: dict) -> ResolvedTarget:
    validate_target_shape(target)
    connection = target["connection"]
    return ResolvedTarget(
        protocol=target["protocol"], target_alias=target["target_alias"],
        model_family=target["model_family"],
        address=resolve_private_target(connection["host"], connection["port"]),
        connection=dict(connection),
        evidence_correlation_key=target["evidence_correlation_key"],
    )


def execute_live_exercise(
    *, authorization_path: Path, target_path: Path, ledger_path: Path,
    artifact_root: Path, asset_path: Path | None = None,
) -> Path:
    started = datetime.now(timezone.utc)
    captured: dict = {}
    commit, _dirty = git_identity()

    def capture_scope(target_payload: dict, authorization: dict) -> None:
        scope = authorization_scope(authorization, target_payload)
        captured.update({
            "run_id": authorization["run_id"], "protocol": authorization["protocol"],
            "actions": [item["name"] for item in authorization["actions"]],
            "scope": scope, "scope_sha256": authorization_scope_sha256(scope),
            "model_family": target_payload["model_family"],
            "target_correlation_sha256": scope["target_correlation_sha256"],
        })

    def factory(target_payload: dict, authorization: dict) -> ActiveBackend:
        # This is the first callback after atomic authorization consumption.
        # Capture the non-sensitive evidence scope before DNS or backend setup can fail.
        capture_scope(target_payload, authorization)
        target = _resolved(target_payload)
        sensitive_values = SensitiveValueRegistry.from_target(target_payload, target.address)
        if not sensitive_values.values:
            raise ValueError("active target identity registry is empty")
        remote_name = canonical_remote_name(target.protocol, authorization["nonce"])
        protected_asset = None
        if set(captured["actions"]) & ASSET_ACTIONS:
            if asset_path is None:
                raise ValueError("authorized upload/start requires --asset")
            protected_asset, _extension = validate_test_asset(
                asset_path, protocol=target.protocol,
                expected_sha256=authorization["test_asset_sha256"],
                repository_root=ROOT, artifact_root=artifact_root,
            )
        if target.protocol == "bambu":
            return BambuLiveSession(target, remote_name, protected_asset).backend()
        if target.protocol == "moonraker":
            return MoonrakerLiveSession(target, remote_name, protected_asset).backend()
        if target.protocol == "prusalink":
            return PrusaLinkLiveSession(target, remote_name, protected_asset).backend()
        if target.protocol == "elegoo":
            return ElegooLiveSession(target).backend()
        raise ValueError("unsupported active protocol")

    results: list[dict[str, str]] = []
    terminal_failure: BaseException | None = None
    try:
        execute_authorized(
            authorization_path=authorization_path, target_path=target_path,
            ledger_path=ledger_path, backend_factory=factory, expected_commit=commit,
            artifact_root=artifact_root, result_sink=results,
        )
    except BaseException as exc:
        if not captured and isinstance(exc, AuthorizationConsumedError):
            capture_scope(exc.target, exc.authorization)
        if not captured:
            raise
        terminal_failure = exc
    ended = datetime.now(timezone.utc)
    passed = sum(item["status"] == "pass" for item in results)
    failed = sum(item["status"] == "fail" for item in results)
    assertions = [{
        "id": f"action_{item['action']}", "status": item["status"],
        "reason_code": "transition_observed" if item["status"] == "pass" else "action_failed",
        "duration_ms": 0.0,
    } for item in results]
    if terminal_failure is not None:
        assertions.append({
            "id": "active_failure", "status": "fail",
            "reason_code": "unexpected_failure", "duration_ms": 0.0,
        })
        failed += 1
    status = (
        "pass"
        if terminal_failure is None and results and passed == len(results) == len(captured["actions"])
        else "fail"
    )
    result = {
        "schema_version": 1, "run_id": captured["run_id"], "git_commit": commit,
        "mode": "exercise", "protocol": captured["protocol"],
        "certification_level": "live_authorized_exercise", "status": status,
        "started_at": utc_iso(started), "ended_at": utc_iso(ended),
        "authorization_scope": captured["scope"],
        "authorization_scope_sha256": captured["scope_sha256"],
        "requested_actions": captured["actions"],
        "executed_actions": [item["action"] for item in results],
        "assertion_counts": {
            "executed": len(assertions), "passed": passed, "failed": failed,
            "blocked": 0, "skipped": 0, "xfailed": 0,
        },
        "metrics": {
            "valid_sample_count": 0, "freshness_seconds": 0.0,
            "model_family": captured["model_family"], "capabilities": [],
            "target_correlation_sha256": captured["target_correlation_sha256"],
        },
        "assertions": assertions,
    }
    published = publish_result(result, artifact_root)
    if terminal_failure is not None and not isinstance(terminal_failure, Exception):
        raise terminal_failure
    return published
