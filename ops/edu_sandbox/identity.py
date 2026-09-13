"""Sandbox identity, path, and resource-name validation."""

from __future__ import annotations

import re
from pathlib import Path

from .errors import ValidationError

SANDBOX_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,47}$")
RESOURCE_PREFIX = "odin-edu-"


def validate_sandbox_id(value: str) -> str:
    if not isinstance(value, str) or not SANDBOX_ID_PATTERN.fullmatch(value):
        raise ValidationError(
            "sandbox ID must match ^[a-z0-9][a-z0-9-]{2,47}$"
        )
    return value


def compose_project(sandbox_id: str) -> str:
    return RESOURCE_PREFIX + validate_sandbox_id(sandbox_id)


def validate_state_root(value: Path) -> Path:
    root = value.expanduser()
    if not root.is_absolute():
        raise ValidationError("state root must be an absolute path")
    resolved = root.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ValidationError("state root cannot be a filesystem root")
    return resolved


def sandbox_directory(state_root: Path, sandbox_id: str) -> Path:
    root = validate_state_root(state_root)
    candidate = (root / validate_sandbox_id(sandbox_id)).resolve(strict=False)
    if candidate.parent != root:
        raise ValidationError("sandbox directory escaped the validated state root")
    return candidate


def assert_path_owned(path: Path, sandbox_dir: Path) -> Path:
    """Resolve a known lifecycle path and prove it remains under its sandbox."""
    resolved_dir = sandbox_dir.resolve(strict=False)
    resolved = path.resolve(strict=False)
    if resolved == resolved_dir or resolved_dir not in resolved.parents:
        raise ValidationError("path is outside the validated sandbox directory")
    return resolved
