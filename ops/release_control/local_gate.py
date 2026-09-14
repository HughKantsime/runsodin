"""Run the deterministic local release-control acceptance suite."""

from __future__ import annotations

import os
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .report import render
from .run_gate import execute

ROOT = Path(__file__).parents[2]


def _docker_host() -> str:
    configured = os.getenv("DOCKER_HOST")
    if configured:
        return configured
    try:
        context = json.loads(subprocess.check_output(["docker", "context", "inspect"], text=True))
        return str(context[0]["Endpoints"]["docker"]["Host"])
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError, IndexError):
        return ""


def main() -> int:
    run_id = os.getenv("RELEASE_CONTROL_RUN_ID") or datetime.now(timezone.utc).strftime("local-%Y%m%dT%H%M%SZ")
    configured = os.getenv("RELEASE_CONTROL_COMPONENT_DIR")
    directory = Path(configured) / "release-control-local" if configured else ROOT / "artifacts/release-control-local" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    junit = directory / "junit.xml"
    installer_root = directory / "installer"
    docker_host = _docker_host()
    result = execute(
        gate_id="release_control_local",
        command=["bash", "-c", "python3.11 -m pytest tests/test_release_control/ -q --tb=short "
                 "-o xfail_strict=true --junitxml={junit} && python3.11 -m ops.release_control.installer_smoke"],
        output=directory / "result.json", timeout=2700, junit=junit,
        expected_artifacts=[f"{installer_root.relative_to(ROOT)}/*/manifest.json"],
        json_status=["manifest.json=pass"],
        environment_allowlist=("ODIN_INSTALL_SMOKE_ARTIFACT_ROOT", "DOCKER_HOST"),
        environment_overrides={
            "ODIN_INSTALL_SMOKE_ARTIFACT_ROOT": str(installer_root),
            **({"DOCKER_HOST": docker_host} if docker_host else {}),
        },
    )
    render(result, directory / "index.html")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
