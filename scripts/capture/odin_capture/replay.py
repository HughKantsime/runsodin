"""Replay control — wraps `backend/modules/printers/telemetry/demo_cli.py`
so capture scenes can declare a scenario name and have the publisher
replay the right fixture at the right speed.

Wake 1: name lookup helpers + command-builder, no subprocess execution.
Wake 2: spawns the publisher in-process or via the docker-compose
publisher container env (preferred — keeps the replay behavior
identical to what App Reviewers see at demo.subsystem.app).
"""

from __future__ import annotations

from pathlib import Path

from .config import REPO_ROOT


SCENARIOS_DIR = REPO_ROOT / "demo_scenarios"


def list_scenarios() -> list[str]:
    """Return scenario names available under demo_scenarios/."""
    if not SCENARIOS_DIR.exists():
        return []
    return sorted(p.name for p in SCENARIOS_DIR.iterdir() if p.is_dir())


def scenario_exists(name: str) -> bool:
    return (SCENARIOS_DIR / name).is_dir()


def build_publisher_env(scenario: str, *, speed: float = 1.0) -> dict[str, str]:
    """Build the env block that the docker-compose publisher container
    consumes (ODIN_DEMO_FIXTURE, ODIN_DEMO_SPEED). Wake 2 callers thread
    this into `docker compose up`.
    """
    if not scenario_exists(scenario):
        raise KeyError(f"unknown scenario: {scenario!r}; valid: {list_scenarios()}")
    fixture_rel = f"tests/fixtures/telemetry/{scenario}.demo.jsonl"
    return {
        "ODIN_DEMO_FIXTURE": fixture_rel,
        "ODIN_DEMO_SPEED": str(speed),
    }
