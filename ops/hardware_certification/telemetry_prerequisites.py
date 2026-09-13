"""Derive Telemetry V2 checklist rows from verified Bambu evidence only."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import validate as validate_schema

from .artifact import atomic_write, git_identity, sha256, utc_iso
from .evidence import EvidenceError, verify_artifact


SCHEMA = Path(__file__).with_name("schemas") / "telemetry-v2-prerequisites.schema.json"


def _row(status: str, source: str | None) -> dict:
    return {"status": status, "source_artifact_sha256": source}


def _action_status(result: dict | None, required: set[str]) -> str:
    if result is None:
        return "blocked"
    actions = {
        item["id"].removeprefix("action_"): item["status"]
        for item in result["assertions"] if item["id"].startswith("action_")
    }
    if not required <= set(actions):
        return "blocked"
    return "pass" if all(actions[name] == "pass" for name in required) else "fail"


def build_prerequisites(
    *, observe_artifact: Path, output: Path,
    exercise_artifact: Path | None = None,
) -> dict:
    commit, _dirty = git_identity()
    _observe_manifest, observe_results = verify_artifact(
        observe_artifact, expected_mode="observe", expected_protocol="bambu",
    )
    observe_result = observe_results.get("bambu")
    if observe_result is None:
        raise EvidenceError("Bambu observe result is missing")
    observe_hash = sha256(observe_artifact / "manifest.json")
    exercise_result = None
    exercise_hash = None
    if exercise_artifact is not None:
        _exercise_manifest, exercise_results = verify_artifact(
            exercise_artifact, expected_mode="exercise", expected_protocol="bambu",
        )
        exercise_result = exercise_results.get("bambu")
        if exercise_result is None:
            raise EvidenceError("Bambu exercise result is missing")
        observe_metrics = observe_result.get("metrics", {})
        exercise_metrics = exercise_result.get("metrics", {})
        if (
            not observe_metrics.get("target_correlation_sha256")
            or observe_metrics.get("target_correlation_sha256")
            != exercise_metrics.get("target_correlation_sha256")
            or not observe_metrics.get("model_family")
            or observe_metrics.get("model_family") != exercise_metrics.get("model_family")
        ):
            raise EvidenceError("Bambu evidence target or model identity is incoherent")
        exercise_hash = sha256(exercise_artifact / "manifest.json")
    payload = {
        "schema_version": 1, "git_commit": commit, "generated_at": utc_iso(),
        "prerequisites": {
            "live_status": _row(observe_result["status"], observe_hash),
            "pause_resume_stop": _row(_action_status(exercise_result, {"pause", "resume", "stop"}), exercise_hash),
            "ams_sync": _row("blocked", None),
            "dispatch": _row(_action_status(exercise_result, {"upload", "start"}), exercise_hash),
            "database_alert_transitions": _row("blocked", None),
            "seven_day_staging_soak": _row("blocked", None),
        },
    }
    validate_schema(payload, json.loads(SCHEMA.read_text(encoding="utf-8")))
    if output.exists():
        raise EvidenceError("Telemetry V2 prerequisite output already exists")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(output, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload
