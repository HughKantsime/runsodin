"""Local stack orchestration — boot/teardown the docker-compose demo stack
that the capture pipeline needs in `--target local` mode.

Reuses `ops/demo/docker-compose.demo.yml` as the source of truth (mosquitto +
odin + replay publisher). Scenario selection is honored by setting
`ODIN_DEMO_SCENARIO` in the publisher's env before `compose up` — that var
flips `ops/demo/demo_publisher.py` from single-printer mode (the App
Reviewer default) into scenario mode (multi-printer fan-out).

## What this module does

* `start_local_stack(scenario)` — runs `docker compose up -d` against the
  demo compose file with `ODIN_DEMO_SCENARIO` injected, then polls
  `/health` until it returns 200.
* `stop_local_stack(handle)` — `docker compose down`. By default volumes
  are preserved across runs (faster re-capture); pass `wipe_volumes=True`
  on the handle to teardown clean.
* `wait_for_health(url, timeout)` — polls a URL until it returns 200.
* `bootstrap_scenario_printers(base_url, scenario_name, access_token)` —
  POSTs each scenario printer to `/api/printers` so the backend has rows
  for the publisher's MQTT topics to bind against. Idempotent: 400 with
  "already exists" is treated as success.
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


def _load_scenario_printers(scenario_name: str) -> list[dict]:
    """Parse `demo_scenarios/<name>/scenario.yaml` directly with PyYAML.

    Bypasses `backend.modules.printers.telemetry.demo` (which transitively
    imports `paho.mqtt.client` via `live_replay.py`) so the capture
    pipeline doesn't pick up backend-runtime dependencies just to read
    the scenario manifest.
    """
    import yaml  # PyYAML, declared in scripts/capture/requirements.txt
    path = REPO_ROOT / "demo_scenarios" / scenario_name / "scenario.yaml"
    if not path.exists():
        raise StackError(f"scenario manifest missing: {path}")
    raw = yaml.safe_load(path.read_text())
    printers = raw.get("printers") or []
    if not printers:
        raise StackError(f"scenario {scenario_name!r} has no printers")
    # Sanity-check the keys the bootstrap step depends on. If a future
    # scenario YAML drops one of these, we want to halt early with a
    # clear message rather than POST a malformed body.
    required_keys = {"id", "serial", "model"}
    for p in printers:
        missing = required_keys - p.keys()
        if missing:
            raise StackError(
                f"scenario {scenario_name!r} printer {p.get('id', '?')!r} "
                f"missing required keys: {sorted(missing)}. The capture "
                f"pipeline needs id+serial+model to build the /api/printers "
                f"POST body."
            )
    return printers


def bootstrap_scenario_printers(
    base_url: str,
    scenario_name: str,
    *,
    access_token: str,
    timeout: float = 10.0,
) -> int:
    """Seed each scenario printer into ODIN's `printers` table via REST.

    For each printer entry in `demo_scenarios/<name>/scenario.yaml`, POSTs
    to `/api/printers` with:
      * `name`         — `printer.id` (e.g. `a1-01`)
      * `model`        — `printer.model` (e.g. `A1`, `H2D`)
      * `api_type`     — `"bambu"`
      * `api_host`     — `"mosquitto"` — the in-cluster compose service
                         the publisher publishes to. ODIN's broker policy
                         resolver (broker_policy.py) sees this hostname
                         in `ODIN_BAMBU_INSECURE_BROKER_HOSTS` and
                         downgrades to plain:1883 instead of TLS:8883.
                         For real Bambu printers this would be the LAN IP.
      * `api_key`      — `<serial>|<placeholder-access-code>` plaintext —
                         the backend encrypts via `crypto.encrypt` and
                         later splits on `|` to get serial + access code.
                         For fixture replay, access code isn't validated
                         (mosquitto.conf has `allow_anonymous true`).
      * `slot_count`   — `4` (default)

    Idempotency: a 400 with `"already exists"` in the body is treated as
    success (re-running the capture pipeline against an existing volume
    shouldn't fail). Any other 4xx/5xx halts with the response envelope.

    Returns the number of printers successfully seeded (newly-created or
    already-existing).
    """
    printers = _load_scenario_printers(scenario_name)
    seeded = 0
    for p in printers:
        body = {
            "name": p["id"],
            "model": p["model"],
            "api_type": "bambu",
            # Must match an entry in `ODIN_BAMBU_INSECURE_BROKER_HOSTS`
            # so the broker_policy resolver downgrades to plain:1883 for
            # this printer. The compose stack publishes to `mosquitto:1883`.
            "api_host": "mosquitto",
            "api_key": f"{p['serial']}|capture-pipeline-placeholder",
            "slot_count": 4,
            "is_active": True,
        }
        ok = _post_printer(base_url, body, access_token=access_token, timeout=timeout)
        if ok:
            seeded += 1
            logger.info(
                "seeded printer name=%s serial=%s model=%s",
                p["id"], p["serial"], p["model"],
            )
    if seeded != len(printers):
        # Should never hit — _post_printer raises on hard fail. Keep the
        # invariant so a future code change doesn't silently under-seed.
        raise StackError(
            f"seeded {seeded}/{len(printers)} printers — partial bootstrap "
            f"is a hard halt to avoid producing a half-empty fleet capture."
        )
    return seeded


def _post_printer(
    base_url: str,
    body: dict,
    *,
    access_token: str,
    timeout: float,
) -> bool:
    """POST one printer to /api/printers. Returns True on 201 or
    already-exists 400. Raises StackError on any other failure.

    `/api/printers` requires the X-API-Key header (not JWT bearer).
    The compose stack injects ODIN_DEMO_API_KEY into the backend's
    API_KEY env var; we read the same value here so the bootstrap
    posts hit the auth-allowed path instead of 401.
    """
    import json
    import os
    import urllib.error
    import urllib.request

    api_key = os.environ.get("ODIN_DEMO_API_KEY") or "capture-pipeline-local"
    url = f"{base_url.rstrip('/')}/api/printers"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True
            raise StackError(
                f"POST {url} unexpected status {resp.status} "
                f"for printer={body['name']!r}"
            )
    except urllib.error.HTTPError as e:
        envelope = e.read().decode("utf-8", "replace")
        if e.code == 400 and "already exists" in envelope.lower():
            logger.info("printer %s already exists — idempotent re-seed", body["name"])
            return True
        raise StackError(
            f"POST {url} failed for printer={body['name']!r}: "
            f"HTTP {e.code} {envelope[:300]}"
        )
