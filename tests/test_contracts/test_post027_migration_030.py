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


def _ledger(engine):
    with engine.connect() as c:
        return {row[0] for row in c.execute(text("SELECT migration_id FROM odin_schema_migrations"))}


def _warnings(engine):
    with engine.connect() as c:
        value = c.execute(text("SELECT value FROM system_config WHERE key='schema_deferred_migrations'")).scalar()
    return json.loads(value) if isinstance(value, str) else value


def test_duplicate_links_defer_030_boot_continues_and_later_boots_retry(tmp_path):
    """1.9.x had no constraint, so duplicates can exist: never boot-loop, never rewrite."""
    engine = _upgrade_027_database(tmp_path, duplicates=True)
    try:
        first = bootstrap_database(engine)
        assert M.MIGRATION_ID not in first["applied"] and first["deferred"] == [M.MIGRATION_ID]
        assert _index_sql(engine) is None
        assert M.MIGRATION_ID not in _ledger(engine) and M.MIGRATION_ID + "#deferred" in _ledger(engine)
        warning = _warnings(engine)[M.MIGRATION_ID]
        assert warning["detail"]["spoolman_spool_ids"] == [5] and warning["detail"]["spool_ids"] == [1, 3]
        assert "unlink" in warning["reason"].lower() and "restart" in warning["reason"].lower()
        with engine.connect() as c:
            assert c.execute(text("SELECT spoolman_spool_id FROM spools ORDER BY id")).scalars().all() == [5, 6, 5]
        # Still deferred on the next boot; the schema stays valid and stable.
        second = bootstrap_database(engine)
        assert second["applied"] == [] and second["deferred"] == [M.MIGRATION_ID]
        assert second["schema_fingerprint"] == first["schema_fingerprint"]
        # The operator unlinks one spool; the next boot creates the index and clears the warning.
        with engine.begin() as c:
            c.execute(text("UPDATE spools SET spoolman_spool_id=NULL WHERE id=3"))
        third = bootstrap_database(engine)
        assert third["applied"] == [M.MIGRATION_ID] and third["deferred"] == []
        assert _index_sql(engine) == M.DDL
        assert M.MIGRATION_ID in _ledger(engine) and M.MIGRATION_ID + "#deferred" not in _ledger(engine)
        assert not _warnings(engine)
    finally:
        engine.dispose()


def test_deferred_migration_is_reported_by_readiness_without_failing_it(tmp_path):
    from modules.system.schema_warnings import deferred_migrations
    engine = _upgrade_027_database(tmp_path, duplicates=True)
    try:
        bootstrap_database(engine)
        with engine.connect() as c:
            public = deferred_migrations(c, detail=False)
            admin = deferred_migrations(c, detail=True)
        assert public == [{"migration": M.MIGRATION_ID}]
        assert admin[0]["migration"] == M.MIGRATION_ID and admin[0]["detail"]["spool_ids"] == [1, 3]
    finally:
        engine.dispose()


def test_non_deferrable_migrations_cannot_defer(tmp_path, monkeypatch):
    from core.schema import bootstrap
    from core.schema.migrator import MigrationDeferred
    engine = _upgrade_027_database(tmp_path, duplicates=False)
    monkeypatch.setattr(M, "DEFERRABLE", False)
    monkeypatch.setattr(M, "apply", lambda connection: (_ for _ in ()).throw(MigrationDeferred("x", detail={})))
    try:
        with pytest.raises(MigrationError):
            bootstrap.bootstrap_database(engine)
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
