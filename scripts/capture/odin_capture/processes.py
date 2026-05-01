"""Local stack orchestration — boot/teardown the docker-compose demo stack
that the capture pipeline needs in `--target local` mode.

Reuses `ops/demo/docker-compose.demo.yml` as the source of truth (mosquitto +
odin + replay publisher). Scenario selection is honored by setting
`ODIN_DEMO_SCENARIO` in the publisher's env before `compose up` — that var
flips `ops/demo/demo_publisher.py` from single-printer mode (the App
Reviewer default) into scenario mode (multi-printer fan-out).

## What this module does (and doesn't)

* `start_local_stack(scenario)` — runs `docker compose up -d` against the
  demo compose file with `ODIN_DEMO_SCENARIO` injected, then polls
  `/health` until it returns 200.
* `stop_local_stack(handle)` — `docker compose down`. By default volumes
  are preserved across runs (faster re-capture); pass `wipe_volumes=True`
  on the handle to teardown clean.
* `wait_for_health(url, timeout)` — polls a URL until it returns 200.
* `bootstrap_scenario_printers(...)` — **deliberately unimplemented**. See
  the docstring for the gap and the path forward.

## The bootstrap gap

ODIN does not auto-register printers from MQTT telemetry — the dashboard
only renders printers that already have a row in the `printers` table.
The App Reviewer demo solves this by shipping a pre-baked SQLite DB on a
PVC. For capture-pipeline `--target local` we need either:

  (a) **A scenario-seed script** that POSTs each printer via
      `/api/printers` (preferred — keeps every dependency in code).
  (b) **MQTT auto-registration** added to `ingest.py` (broader feature,
      lands separately).
  (c) **Per-scenario pre-baked DBs** dropped into the volume mount
      (fragile — drifts with schema).

Wake 2 ships (a)'s plumbing as `bootstrap_scenario_printers` but leaves
the body for the next heartbeat. The CLI surfaces the gap loudly when
`scene` is invoked against `--target local`, naming the unblock.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen

from .config import REPO_ROOT

logger = logging.getLogger("odin_capture.processes")


COMPOSE_FILE = REPO_ROOT / "ops" / "demo" / "docker-compose.demo.yml"
COMPOSE_PROJECT = "odin-demo"
HEALTH_URL_LOCAL = "http://localhost:8000/health"


@dataclass
class StackHandle:
    """Reference to a running local stack so the caller can stop it."""
    compose_file: Path
    project_name: str = COMPOSE_PROJECT
    scenario: Optional[str] = None
    base_url: str = "http://localhost:8000"
    started_at: Optional[float] = None
    extra_env: dict[str, str] = field(default_factory=dict)
    wipe_volumes: bool = False


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


def _compose_cmd(handle: StackHandle, *args: str) -> list[str]:
    return [
        "docker", "compose",
        "-f", str(handle.compose_file),
        "-p", handle.project_name,
        *args,
    ]


def _build_env(scenario: Optional[str], extra: dict[str, str]) -> dict[str, str]:
    """Compose the env dict passed to `docker compose up`. The compose
    file inherits this env (per docker-compose's default behavior) and
    threads the relevant vars into the publisher service.
    """
    env = os.environ.copy()
    if scenario:
        env["ODIN_DEMO_SCENARIO"] = scenario
    env.update(extra)
    # The compose file requires these — fail loud here rather than later
    # inside docker. Use deterministic capture-pipeline values when not
    # already set (these never reach a prod or App Review env).
    env.setdefault("ODIN_DEMO_ENCRYPTION_KEY", _ephemeral_fernet_key())
    env.setdefault("ODIN_DEMO_JWT_SECRET_KEY", _ephemeral_jwt_secret())
    env.setdefault("ODIN_DEMO_API_KEY", "capture-pipeline-local")
    env.setdefault("ODIN_DEMO_PUBLIC_URL", "http://localhost:8000")
    env.setdefault("ODIN_DEMO_HOST_IP", "127.0.0.1")
    return env


def _ephemeral_fernet_key() -> str:
    """Fresh Fernet key for one capture run. Never reused, never committed."""
    try:
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()
    except ImportError:
        # Fall back to a deterministic-but-clearly-fake key if cryptography
        # isn't available locally. The compose stack will refuse to start
        # without a real key, so this only matters for dry runs.
        return "DEV_ONLY_NOT_A_REAL_KEY_REPLACE_BEFORE_RUN========="


def _ephemeral_jwt_secret() -> str:
    import secrets
    return secrets.token_urlsafe(32)


def start_local_stack(
    scenario: str,
    *,
    extra_env: Optional[dict[str, str]] = None,
    health_timeout: int = 60,
    dry_run: bool = False,
) -> StackHandle:
    """Bring up the demo stack with the given scenario fixture.

    On `dry_run=True`, validates preconditions (docker available, compose
    file present) without mutating system state. Used by `cli validate`.
    """
    if not COMPOSE_FILE.exists():
        raise StackError(f"compose file missing: {COMPOSE_FILE}")
    if not _docker_compose_available():
        raise StackError(
            "docker compose v2 not available on PATH. "
            "Install Docker Desktop / Docker Engine v20.10+."
        )

    handle = StackHandle(
        compose_file=COMPOSE_FILE,
        scenario=scenario,
        started_at=time.time(),
        extra_env=extra_env or {},
    )

    if dry_run:
        return handle

    env = _build_env(scenario, handle.extra_env)
    logger.info("docker compose up -d (project=%s scenario=%s)", handle.project_name, scenario)
    proc = subprocess.run(
        _compose_cmd(handle, "up", "-d"),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise StackError(
            f"docker compose up failed (exit {proc.returncode}):\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )

    if not wait_for_health(HEALTH_URL_LOCAL, timeout=health_timeout):
        # Capture compose state for the error message before raising —
        # the caller will tear down via stop_local_stack.
        ps = subprocess.run(_compose_cmd(handle, "ps"), capture_output=True, text=True, env=env)
        raise StackError(
            f"stack didn't reach /health within {health_timeout}s. "
            f"compose ps:\n{ps.stdout}"
        )

    return handle


def stop_local_stack(handle: StackHandle) -> None:
    """Tear down the demo stack referenced by `handle`."""
    if not _docker_compose_available():
        # Nothing we can do; let the caller's finally-block exit cleanly.
        return
    args = ["down"]
    if handle.wipe_volumes:
        args.extend(["-v", "--remove-orphans"])
    logger.info("docker compose %s (project=%s)", " ".join(args), handle.project_name)
    subprocess.run(
        _compose_cmd(handle, *args),
        env=_build_env(handle.scenario, handle.extra_env),
        capture_output=True,
        text=True,
        check=False,
    )


def wait_for_health(url: str = HEALTH_URL_LOCAL, *, timeout: int = 60) -> bool:
    """Poll a /health endpoint until it returns 200 or timeout."""
    deadline = time.monotonic() + timeout
    last_err: Optional[str] = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=5) as resp:
                if 200 <= resp.status < 300:
                    return True
                last_err = f"http {resp.status}"
        except (URLError, ConnectionRefusedError, TimeoutError, OSError) as e:
            last_err = str(e)
        time.sleep(2)
    logger.warning("health-check timeout on %s after %ds (last error: %s)", url, timeout, last_err)
    return False


def bootstrap_scenario_printers(base_url: str, scenario_name: str, *, access_token: str) -> int:
    """Seed each scenario printer into ODIN's `printers` table via REST.

    **Wake 2 status: deliberately unimplemented.** See module docstring
    for the gap. The signature is locked in so Wake 2b can drop in the
    body without touching callers. Preferred path (see option (a)):

    1. Load `DemoScenario` for `scenario_name`
    2. For each printer, POST to `/api/printers` with
       `{name: printer.id, model: printer.model, api_type: "bambu",
         api_host: "0.0.0.0", slot_count: 4}` — telemetry by-serial then
       hydrates the row.
    3. Verify each by `GET /api/printers/{id}`.

    Returns the number of printers seeded. Raises StackError on failure.
    """
    raise StackError(
        "bootstrap_scenario_printers: Wake 2b deliverable. The capture "
        "pipeline cannot run end-to-end against `--target local` until "
        "scenario printers are seeded into the ODIN DB. Tracking: ODIN-142."
    )
