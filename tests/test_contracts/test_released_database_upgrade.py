"""F22-22.3-01: a database written by a published release image upgrades with current source.

tests/fixtures/released_databases/odin-v1.9.24-sqlite.sql is the exact first-boot SQLite
database of the published ghcr.io/hughkantsime/odin:v1.9.24 image (schema + migration
ledger, see the file header). Every published image v1.9.13-v1.9.24 records the same
orm-baseline:1 checksum. The container entrypoint's own bootstrap command
(``ODIN_DB_ROLE=bootstrap python3 -m scripts.bootstrap_database``) must upgrade it, then be
idempotent. The catalog previously pinned only a local pre-release build's manifest, so
this database was refused with "Migration history mismatch: orm-baseline:1".
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FIXTURES = ROOT / "tests/fixtures/released_databases"
CATALOG = json.loads((BACKEND / "core/schema/orm_baselines.json").read_text())
PUBLISHED_ORM_CHECKSUM = "36d66151e15f6a2534ddc98e5f3bf4049a9796570197e32d227e868ab93cab0b"


def _load(fixture: str, db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript((FIXTURES / fixture).read_text())
    conn.close()


def _ledger(db: Path) -> dict[str, tuple[str, str, str]]:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT migration_id, checksum, dialect, applied_at FROM odin_schema_migrations").fetchall()
    conn.close()
    return {r[0]: tuple(r[1:]) for r in rows}


def _bootstrap(db: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "DATABASE_PATH": str(db), "ODIN_DB_ROLE": "bootstrap",
           "ODIN_DATA_DIR": str(db.parent), "JWT_SECRET_KEY": "released-db-test-only-" + uuid.uuid4().hex}
    return subprocess.run([sys.executable, "-m", "scripts.bootstrap_database"], cwd=BACKEND, env=env,
                          capture_output=True, text=True, timeout=900)


def test_fixture_is_the_published_v1_9_24_first_boot_database(tmp_path):
    db = tmp_path / "odin.db"
    _load("odin-v1.9.24-sqlite.sql", db)
    ledger = _ledger(db)
    assert ledger["orm-baseline:1"][:2] == (PUBLISHED_ORM_CHECKSUM, "sqlite")
    assert len(ledger) == 26 and "python:003-education-monitor-claims" in ledger
    # recorded by the release, not by current source
    assert "python:004-quiet-hours-scoped-claims" not in ledger


def test_catalog_recognizes_the_published_release_baseline():
    published = [k for k, v in CATALOG.items() if v["checksum"] == PUBLISHED_ORM_CHECKSUM]
    assert published == ["published_1_9_13_to_1_9_24"]
    assert "ghcr.io/hughkantsime/odin:v1.9.24@sha256:" in CATALOG[published[0]]["image"]


@pytest.mark.parametrize("fixture", ["odin-v1.9.24-sqlite.sql"])
def test_published_release_database_upgrades_with_current_source_and_is_idempotent(tmp_path, fixture):
    db = tmp_path / "odin.db"
    _load(fixture, db)
    before = _ledger(db)
    first = _bootstrap(db)
    assert first.returncode == 0, first.stderr[-3000:]
    result = json.loads(first.stdout.strip().splitlines()[-1])
    assert "python:027-power-target-operations" in result["applied"], result["applied"]
    after = _ledger(db)
    # the release's own history is preserved byte-for-byte, nothing is re-recorded
    assert {k: after[k] for k in before} == before
    assert after["orm-baseline:1"][0] == PUBLISHED_ORM_CHECKSUM
    second = _bootstrap(db)
    assert second.returncode == 0, second.stderr[-3000:]
    again = json.loads(second.stdout.strip().splitlines()[-1])
    assert again["applied"] == [] and again["schema_fingerprint"] == result["schema_fingerprint"]
    assert _ledger(db) == after


def test_unrecognized_release_checksum_is_still_refused_before_mutation(tmp_path):
    db = tmp_path / "odin.db"
    _load("odin-v1.9.24-sqlite.sql", db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE odin_schema_migrations SET checksum = ? WHERE migration_id = 'orm-baseline:1'", ("1" * 64,))
    conn.commit()
    schema = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    conn.close()
    result = _bootstrap(db)
    assert result.returncode != 0 and "Migration history mismatch: orm-baseline:1" in result.stderr
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    conn.close()
