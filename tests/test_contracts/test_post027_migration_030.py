"""Migration 030 (first post-027 registration): fresh boot, upgrade, duplicate refusal, idempotence."""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text

from core.schema import bootstrap_database
from core.schema.migrator import MigrationError

ROOT = Path(__file__).resolve().parents[2]
M = importlib.import_module("core.schema.migrations.030_spoolman_link_uniqueness")


def _index_sql(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"), {"n": M.INDEX}).scalar()


def test_fresh_sqlite_boot_applies_030_once_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        first = bootstrap_database(engine)
        assert first["applied"][-2:] == ["python:030-spoolman-link-uniqueness", "orm-baseline:1"]
        assert _index_sql(engine) == M.DDL
        second = bootstrap_database(engine)
        assert second["applied"] == [] and second["schema_fingerprint"] == first["schema_fingerprint"]
        with engine.begin() as c:
            c.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(1,'B','N','PLA')"))
            c.execute(text("INSERT INTO spools(filament_id,spoolman_spool_id) VALUES(1,7),(1,NULL),(1,NULL)"))
        with pytest.raises(Exception, match="UNIQUE"):
            with engine.begin() as c:
                c.execute(text("INSERT INTO spools(filament_id,spoolman_spool_id) VALUES(1,7)"))
    finally:
        engine.dispose()


def _upgrade_027_database(tmp_path, duplicates):
    """A database at the last pre-030 schema: current boot, then 030 removed from it."""
    db = tmp_path / "pre030.db"
    engine = create_engine(f"sqlite:///{db}")
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text(f"DROP INDEX {M.INDEX}"))
        c.execute(text("DELETE FROM odin_schema_migrations WHERE migration_id=:id"), {"id": M.MIGRATION_ID})
        c.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(1,'B','N','PLA')"))
        c.execute(text("INSERT INTO spools(filament_id,spoolman_spool_id) VALUES(1,5),(1,6)"))
        if duplicates:
            c.execute(text("INSERT INTO spools(filament_id,spoolman_spool_id) VALUES(1,5)"))
    return engine


def test_pre030_database_upgrades_and_keeps_its_rows(tmp_path):
    engine = _upgrade_027_database(tmp_path, duplicates=False)
    try:
        result = bootstrap_database(engine)
        assert result["applied"] == ["python:030-spoolman-link-uniqueness"]
        assert _index_sql(engine) == M.DDL
        with engine.connect() as c:
            assert c.execute(text("SELECT spoolman_spool_id FROM spools ORDER BY id")).scalars().all() == [5, 6]
    finally:
        engine.dispose()


def test_duplicate_links_refuse_the_upgrade_without_rewriting_or_ledgering(tmp_path):
    engine = _upgrade_027_database(tmp_path, duplicates=True)
    try:
        with pytest.raises(MigrationError, match="Several spools link the same Spoolman spool"):
            bootstrap_database(engine)
        assert _index_sql(engine) is None
        with engine.connect() as c:
            assert c.execute(text("SELECT spoolman_spool_id FROM spools ORDER BY id")).scalars().all() == [5, 6, 5]
            assert c.execute(text("SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id"), {"id": M.MIGRATION_ID}).scalar_one() == 0
    finally:
        engine.dispose()


def test_altered_index_shape_is_refused(tmp_path):
    engine = _upgrade_027_database(tmp_path, duplicates=False)
    try:
        with engine.begin() as c:
            c.execute(text(f"CREATE UNIQUE INDEX {M.INDEX} ON spools (spoolman_spool_id)"))
        with pytest.raises(MigrationError, match="shape mismatch"):
            bootstrap_database(engine)
    finally:
        engine.dispose()


def test_published_v1_9_24_database_upgrades_through_030_with_the_entrypoint_command(tmp_path):
    db = tmp_path / "odin.db"
    conn = sqlite3.connect(db)
    conn.executescript((ROOT / "tests/fixtures/released_databases/odin-v1.9.24-sqlite.sql").read_text())
    conn.close()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "DATABASE_PATH": str(db), "ODIN_DB_ROLE": "bootstrap",
           "ODIN_DATA_DIR": str(tmp_path), "JWT_SECRET_KEY": "post027-test-only-" + uuid.uuid4().hex}
    runs = [subprocess.run([sys.executable, "-m", "scripts.bootstrap_database"], cwd=ROOT / "backend", env=env,
                           capture_output=True, text=True, timeout=900) for _ in range(2)]
    assert all(run.returncode == 0 for run in runs), runs[0].stderr[-3000:] + runs[1].stderr[-3000:]
    first, second = (json.loads(run.stdout.strip().splitlines()[-1]) for run in runs)
    assert "python:030-spoolman-link-uniqueness" in first["applied"]
    assert second["applied"] == [] and second["schema_fingerprint"] == first["schema_fingerprint"]
    engine = create_engine(f"sqlite:///{db}")
    try:
        assert _index_sql(engine) == M.DDL
    finally:
        engine.dispose()
