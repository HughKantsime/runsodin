"""Durable checks behind the Deployment claims certification (claims-deploy).

Static checks cover what can be asserted without Windows hardware or GitHub
mutation. Two strict xfails pin defects found by executed evidence; they flip
to XPASS (and fail the run) once the defect is fixed so the marker is removed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
PS1 = ROOT / "install" / "install.ps1"
WIN_GUIDE = ROOT / "install" / "WINDOWS_INSTALL.md"
UPDATE_SH = ROOT / "install" / "update.sh"
ONE_LINER = "irm https://raw.githubusercontent.com/HughKantsime/runsodin/master/install/install.ps1 | iex"


def _workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))
    return data


@pytest.mark.parametrize(
    "name",
    ["trusted-validation.yml", "promote.yml", "publish-image.yml", "promote-production.yml"],
)
def test_release_workflows_are_manual_dispatch_on_self_hosted(name):
    data = _workflow(name)
    assert list(data["on"]) == ["workflow_dispatch"], data["on"]
    for job in data["jobs"].values():
        assert "self-hosted" in job["runs-on"]


def test_publication_workflows_target_ghcr_repository():
    for name in ("publish-image.yml", "promote-production.yml"):
        assert "IMAGE_REPOSITORY: ghcr.io/hughkantsime/odin" in (WORKFLOWS / name).read_text(encoding="utf-8")


def test_windows_one_liner_documented_exactly():
    assert ONE_LINER in WIN_GUIDE.read_text(encoding="utf-8")


def test_windows_installer_preflight_steps_present():
    src = PS1.read_text(encoding="utf-8")
    for needle in (
        "[System.Environment]::OSVersion",      # Windows version
        "docker version --format",              # Docker daemon
        "docker compose version",               # Compose
        "wsl --status",                         # WSL2 (warn-only)
        ".PSDrive",                             # disk space
        "Get-NetTCPConnection -LocalPort",      # port availability
    ):
        assert needle in src, needle


def test_windows_installer_config_health_and_browser_order():
    src = PS1.read_text(encoding="utf-8")
    for needle in ("Get-NetIPAddress", "Read-Host", "Get-TimeZone", "install/docker-compose.yml", 'Join-Path $InstallDir ".env"'):
        assert needle in src, needle
    health = src.index("/health/ready")
    assert "$resp.ready -eq $true" in src
    fail_closed = src.index("readiness was not confirmed")
    browser = src.index('Start-Process "http://localhost:$HttpPort"')
    assert health < fail_closed < browser


def test_windows_guide_sections():
    headings = set(re.findall(r"^#{2,3} (.+)$", WIN_GUIDE.read_text(encoding="utf-8"), flags=re.M))
    for section in ("Prerequisites", "Option B: Manual install", "Troubleshooting", "Updating"):
        assert section in headings, section


@pytest.mark.slow
def test_fresh_sqlite_bootstrap_succeeds(tmp_path):
    db = tmp_path / "odin.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db}",
        "DATABASE_PATH": str(db),
        "ODIN_DB_ROLE": "bootstrap",
        "ODIN_DATA_DIR": str(tmp_path),
        "JWT_SECRET_KEY": "claims-deploy-test-only",
    }
    result = subprocess.run(
        [sys.executable, "-m", "scripts.bootstrap_database"],
        cwd=ROOT / "backend", env=env, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: update.sh rollback text says 'docker pull ghcr.io/hughkantsime/odin:v<CURRENT> && "
        "docker compose up -d', but compose runs ${ODIN_IMAGE} (installer writes :latest to .env), so the "
        "pulled version tag is never used."
    ),
)
def test_update_rollback_instruction_targets_the_image_compose_runs():
    src = UPDATE_SH.read_text(encoding="utf-8")
    rollback_lines = [line for line in src.splitlines() if "Rollback" in line or "docker compose up -d" in line]
    assert rollback_lines
    # A working rollback must pin the compose image (e.g. ODIN_IMAGE=...:vX) rather than only pulling a tag.
    assert any("ODIN_IMAGE=" in line for line in rollback_lines), rollback_lines
