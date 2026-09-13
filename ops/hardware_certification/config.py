"""Closed single-target configuration loading for live certification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .security import (
    CANONICAL_ARTIFACT_ROOT, SecurityError, assert_outside_artifact_trees,
    load_protected_json, resolve_private_target,
)


SCHEMA_PATH = Path(__file__).with_name("schemas") / "target-config.schema.json"
DEFAULT_ARTIFACT_ROOT = CANONICAL_ARTIFACT_ROOT


@dataclass(frozen=True)
class ResolvedTarget:
    protocol: str
    target_alias: str
    model_family: str
    address: str
    connection: dict[str, Any]
    evidence_correlation_key: str = ""


def validate_target_shape(target: dict[str, Any]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if list(Draft202012Validator(schema).iter_errors(target)):
        raise SecurityError("target config fields are invalid")
    connection = target.get("connection", {})
    if target.get("protocol") == "prusalink" and "username" in connection and connection.get("tls"):
        raise SecurityError("PrusaLink Digest certification requires private HTTP or an API key for TLS")


def load_target(path: Path, *, artifact_root: Path = DEFAULT_ARTIFACT_ROOT) -> ResolvedTarget:
    assert_outside_artifact_trees(path, artifact_root)
    target = load_protected_json(path)
    validate_target_shape(target)
    connection = target["connection"]
    address = resolve_private_target(connection["host"], connection["port"])
    return ResolvedTarget(
        protocol=target["protocol"], target_alias=target["target_alias"],
        model_family=target["model_family"], address=address,
        connection=dict(connection),
        evidence_correlation_key=target["evidence_correlation_key"],
    )
