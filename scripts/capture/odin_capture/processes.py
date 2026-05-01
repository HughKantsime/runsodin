"""Local stack orchestration — boot/teardown the docker-compose demo stack
that the capture pipeline needs in `--target local` mode.

Reuses `ops/demo/docker-compose.demo.yml` as the source of truth (mosquitto +
odin + replay publisher). Scenario selection is honored by setting
`ODIN_DEMO_FIXTURE` in the publisher's env before `compose up`.

Wake 1: signature + light implementation. Wake 2 wires it end-to-end with
the first scene; until then `start_local_stack` raises NotImplementedError
when called by anything other than `--dry-run`.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.request import urlopen

from .config import REPO_ROOT


COMPOSE_FILE = REPO_ROOT / "ops" / "demo" / "docker-compose.demo.yml"
HEALTH_URL_LOCAL = "http://localhost:8000/health"


@dataclass
class StackHandle:
    """Reference to a running local stack so the caller can stop it."""
    compose_file: Path
    project_name: str = "odin-demo"
    scenario: Optional[str] = None
    started_at: Optional[float] = None


class StackError(RuntimeError):
    """Raised when stack boot/teardown fails or health doesn't converge."""


def _docker_compose_available() -> bool:
    """True iff `docker compose` (v2) is on PATH."""
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def start_local_stack(scenario: str, *, dry_run: bool = False) -> StackHandle:
    """Bring up the demo stack with the given scenario fixture.

    Wake 2 implements the full subprocess.run("docker compose up -d") path.
    For Wake 1 the function is callable only with dry_run=True, which
    validates preconditions (docker available, compose file present)
    without mutating system state.
    """
    if not COMPOSE_FILE.exists():
        raise StackError(f"compose file missing: {COMPOSE_FILE}")
    if not _docker_compose_available():
        raise StackError("docker compose v2 not available on PATH")
    if dry_run:
        return StackHandle(
            compose_file=COMPOSE_FILE,
            scenario=scenario,
            started_at=time.time(),
        )
    raise NotImplementedError(
        "start_local_stack: Wake 2 deliverable. Use --dry-run to validate "
        "preconditions only in Wake 1."
    )


def stop_local_stack(handle: StackHandle) -> None:
    """Tear down the demo stack referenced by `handle`.

    Wake 2 implements `docker compose down -v` against the handle's
    project name. Wake 1 is a no-op for symmetry.
    """
    return None


def wait_for_health(url: str = HEALTH_URL_LOCAL, *, timeout: int = 60) -> bool:
    """Poll a /health endpoint until it returns 200 or timeout.

    Returns True on success, False on timeout. Used by start_local_stack
    in Wake 2 and by the demo-target smoke test in Wake 4.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=5) as resp:
                if 200 <= resp.status < 300:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False
