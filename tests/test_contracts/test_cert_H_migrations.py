"""Cert group H — F22-22.3-01/02/03 database migrations run by the container entrypoint.

The entrypoint (docker/entrypoint.sh) runs ``ODIN_DB_ROLE=bootstrap python3 -m
scripts.bootstrap_database`` on every start; these tests run exactly that command
against unledgered legacy SQLite databases (tests/fixtures/database_legacy) and a
fresh database, twice, and inspect the resulting real schema. The last test upgrades a
database written by the published ghcr.io/hughkantsime/odin:v1.9.24 image with an
image built from current source (Docker; CERT_H_IMAGE).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
from tests.fixtures.database_legacy import FIXTURE_NAMES, build_fixture  # noqa: E402

CLAIMED_TABLES = ("api_tokens", "active_sessions", "token_blacklist", "quota_usage",
                  "model_revisions", "report_schedules", "timelapses")
CLAIMED_COLUMNS = {
    "users": ("mfa_enabled", "mfa_secret", "quota_grams", "quota_hours", "quota_jobs", "quota_period"),
    "jobs": ("charged_to_user_id", "charged_to_org_id"),
    "groups": ("is_org", "branding_json", "settings_json"),
    "printers": ("org_id", "timelapse_enabled"),
    "models": ("org_id",),
    "spools": ("org_id",),
}


def _entrypoint_bootstrap(db: Path) -> dict:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "DATABASE_PATH": str(db), "ODIN_DB_ROLE": "bootstrap",
           "ODIN_DATA_DIR": str(db.parent), "JWT_SECRET_KEY": "cert-h-test-only-" + uuid.uuid4().hex}
    result = subprocess.run([sys.executable, "-m", "scripts.bootstrap_database"], cwd=BACKEND, env=env,
                            capture_output=True, text=True, timeout=900)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _columns(db: Path) -> dict[str, set[str]]:
    engine = create_engine(f"sqlite:///{db}")
    inspector = inspect(engine)
    out = {t: {c["name"] for c in inspector.get_columns(t)} for t in inspector.get_table_names()}
    engine.dispose()
    return out


def test_fresh_bootstrap_creates_claimed_tables_and_columns_and_is_idempotent(tmp_path):
    db = tmp_path / "odin.db"
    first = _entrypoint_bootstrap(db)
    schema = _columns(db)
    for table in CLAIMED_TABLES:
        assert table in schema, table
    for table, cols in CLAIMED_COLUMNS.items():
        assert set(cols) <= schema[table], (table, set(cols) - schema[table])
    second = _entrypoint_bootstrap(db)
    assert second["applied"] == [] and second["schema_fingerprint"] == first["schema_fingerprint"]


@pytest.mark.parametrize("fixture_name", [n for n in FIXTURE_NAMES if n != "current_noop"])
def test_entrypoint_bootstrap_adds_missing_legacy_columns_idempotently(tmp_path, fixture_name):
    db = tmp_path / f"{fixture_name}.db"
    engine = create_engine(f"sqlite:///{db}")
    build_fixture(engine, BACKEND, fixture_name)
    engine.dispose()
    before = _columns(db)
    first = _entrypoint_bootstrap(db)
    after = _columns(db)
    added = {t: set(cols) - before.get(t, set()) for t, cols in CLAIMED_COLUMNS.items() if t in before}
    for table, cols in CLAIMED_COLUMNS.items():
        assert set(cols) <= after[table], (table, set(cols) - after[table])
    for table in CLAIMED_TABLES:
        assert table in after, table
    if any(added.values()):
        # at least one claimed column was genuinely ALTER-added to a pre-existing legacy table
        assert "python:001-legacy-columns" in first["applied"], first["applied"]
    second = _entrypoint_bootstrap(db)
    assert second["applied"] == [], second["applied"]
    assert second["schema_fingerprint"] == first["schema_fingerprint"]
    assert _columns(db) == after


def test_pre_mfa_fixture_really_lacks_the_claimed_user_columns_before_upgrade(tmp_path):
    db = tmp_path / "pre.db"
    engine = create_engine(f"sqlite:///{db}")
    build_fixture(engine, BACKEND, "pre_mfa_org")
    engine.dispose()
    before = _columns(db)
    assert not ({"mfa_enabled", "mfa_secret", "quota_grams"} & before["users"])
    _entrypoint_bootstrap(db)
    assert {"mfa_enabled", "mfa_secret", "quota_grams", "quota_hours", "quota_jobs", "quota_period"} <= _columns(db)["users"]


IMAGE = os.environ.get("CERT_H_IMAGE", "")
PUBLISHED = os.environ.get("CERT_H_PUBLISHED_IMAGE", "ghcr.io/hughkantsime/odin:v1.9.24")


def _ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=5) as r:
            return json.loads(r.read()).get("ready") is True
    except Exception:
        return False


def _boot(name: str, image: str, volume: str, port: int, timeout: float) -> tuple[bool, str]:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8000", "-v", f"{volume}:/data",
                    "--label", "com.runsodin.cert-h=migration", image], check=True, capture_output=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ready(port):
            return True, ""
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", name], capture_output=True, text=True).stdout.strip()
        if state in {"exited", "dead"}:
            break
        time.sleep(2)
    logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
    return False, (logs.stdout + logs.stderr)[-4000:]


@pytest.mark.live_environment
@pytest.mark.skipif(not IMAGE or shutil.which("docker") is None, reason="CERT_H_IMAGE and docker required")
def test_database_written_by_published_v1_9_24_upgrades_with_current_source_image():
    name = f"certh-mig-{os.getpid()}"
    volume = f"certh-mig-{os.getpid()}"
    subprocess.run(["docker", "volume", "create", "--label", "com.runsodin.cert-h=migration", volume], check=True, capture_output=True)
    log_dir = ROOT / "artifacts/application-certification/cert-H/installer-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        ok, logs = _boot(name, PUBLISHED, volume, 27451, 240)
        assert ok, "published image did not boot: " + logs
        ok, logs = _boot(name, IMAGE, volume, 27451, 240)
        (log_dir / "upgrade-v1.9.24-to-current.log").write_text(logs or "ready")
        assert ok, logs
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
