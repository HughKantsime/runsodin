"""Shared, fail-loud result helpers for EDU readiness gates."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{7,12}$")
GATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
STATUSES = {"pass", "fail", "blocked"}
REQUIRED_FIELDS = {
    "schema_version", "run_id", "gate_id", "mandatory", "status",
    "started_at", "ended_at", "duration_seconds", "tool_versions",
    "executed_count", "skipped_count", "xfailed_count", "metrics",
    "findings", "artifacts",
}


class ResultValidationError(ValueError):
    """Raised when a gate attempts to emit ambiguous evidence."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_result(result: dict[str, Any]) -> None:
    missing = REQUIRED_FIELDS - result.keys()
    extra = result.keys() - REQUIRED_FIELDS
    if missing or extra:
        raise ResultValidationError(f"result fields missing={sorted(missing)} extra={sorted(extra)}")
    if result["schema_version"] != 1:
        raise ResultValidationError("unsupported schema_version")
    if not RUN_ID_RE.fullmatch(str(result["run_id"])):
        raise ResultValidationError("invalid run_id")
    if not GATE_ID_RE.fullmatch(str(result["gate_id"])):
        raise ResultValidationError("invalid gate_id")
    if result["status"] not in STATUSES:
        raise ResultValidationError("invalid status")
    if not isinstance(result["mandatory"], bool):
        raise ResultValidationError("mandatory must be boolean")
    for key in ("executed_count", "skipped_count", "xfailed_count"):
        if not isinstance(result[key], int) or result[key] < 0:
            raise ResultValidationError(f"{key} must be a non-negative integer")
    if result["duration_seconds"] < 0:
        raise ResultValidationError("duration_seconds must be non-negative")
    if result["status"] == "pass" and result["executed_count"] == 0:
        raise ResultValidationError("passing result executed zero checks")
    if result["status"] == "pass" and (result["skipped_count"] or result["xfailed_count"]):
        raise ResultValidationError("passing result contains skips or xfails")
    for artifact in result["artifacts"]:
        path = Path(artifact)
        if path.is_absolute() or ".." in path.parts:
            raise ResultValidationError(f"artifact path must be relative: {artifact}")
    for key in ("tool_versions", "metrics"):
        if not isinstance(result[key], dict):
            raise ResultValidationError(f"{key} must be an object")
    if not isinstance(result["findings"], list) or not all(isinstance(v, str) for v in result["findings"]):
        raise ResultValidationError("findings must be a string array")


def write_result(run_dir: Path, result: dict[str, Any]) -> Path:
    validate_result(result)
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / f"{result['gate_id']}.json"
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=run_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def load_result(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    validate_result(result)
    return result
