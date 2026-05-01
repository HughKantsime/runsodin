"""Capture pipeline configuration.

Resolves repo paths, scene directory, output directory, target backend
URLs, and surfaces them to the rest of the package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CAPTURE_ROOT = REPO_ROOT / "scripts" / "capture"
SCENES_DIR = CAPTURE_ROOT / "scenes"
PRESETS_DIR = CAPTURE_ROOT / "presets"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "captures" / "out"


@dataclass(frozen=True)
class TargetConfig:
    """Where the pipeline points its browser + replay."""
    name: str
    base_url: str
    boots_local_stack: bool


TARGET_LOCAL = TargetConfig(
    name="local",
    base_url=os.environ.get("ODIN_CAPTURE_LOCAL_URL", "http://localhost:8000"),
    boots_local_stack=True,
)

TARGET_DEMO = TargetConfig(
    name="demo",
    base_url=os.environ.get("ODIN_CAPTURE_DEMO_URL", "https://demo.subsystem.app"),
    boots_local_stack=False,
)

TARGETS: dict[str, TargetConfig] = {
    "local": TARGET_LOCAL,
    "demo": TARGET_DEMO,
}


def resolve_target(name: str) -> TargetConfig:
    """Look up a target by name. Raises KeyError on unknown."""
    if name not in TARGETS:
        raise KeyError(f"unknown target: {name!r}; valid: {sorted(TARGETS)}")
    return TARGETS[name]
