"""Deterministic identity for code that creates or verifies certification evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _tree_sha256(roots: tuple[Path, ...], suffixes: set[str]) -> str:
    files: list[Path] = []
    for root in roots:
        files.extend(root.rglob("*") if root.is_dir() else [root])
    digest = hashlib.sha256()
    for path in sorted(
        item for item in files
        if item.is_file() and item.suffix in suffixes and "__pycache__" not in item.parts
    ):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def certification_implementation_sha256() -> str:
    """Hash every checked-in parser, transport, exercise, and evidence implementation."""
    roots = (
        ROOT / "ops" / "hardware_certification",
        ROOT / "backend" / "modules" / "printers" / "parsing",
        ROOT / "backend" / "modules" / "printers" / "printer_models.py",
        ROOT / "backend" / "modules" / "printers" / "telemetry" / "bambu",
        ROOT / "backend" / "modules" / "printers" / "telemetry" / "events.py",
        ROOT / "backend" / "modules" / "printers" / "telemetry" / "state.py",
        ROOT / "backend" / "modules" / "printers" / "telemetry" / "transition.py",
    )
    return _tree_sha256(roots, {".py", ".json", ".txt"})


def certification_fixture_sha256() -> str:
    """Hash the exact replay tests and fictional fixtures verified by the runner."""
    return _tree_sha256((
        ROOT / "tests" / "hardware_certification",
        ROOT / "tests" / "hardware" / "test_read_only_certification.py",
        ROOT / "tests" / "hardware" / "fixtures",
    ), {".py", ".json", ".txt"})


def certification_simulator_sha256() -> str:
    """Hash only the loopback peer implementation used to make replay claims."""
    return _tree_sha256((ROOT / "ops" / "hardware_certification" / "simulators.py",), {".py"})
