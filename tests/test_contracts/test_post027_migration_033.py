"""Migration 033: timelapse capture tenancy (additive side table).

Fresh boot, upgrade of an earlier database and of the published v1.9.24 database,
exact-shape refusal and backup inventory. Rows follow their ownership anchor
(ON DELETE CASCADE); anchors are never deleted by ODIN, so there is no separate
user/org cleanup or retention. The PostgreSQL proof is run_post027_restore.py.
"""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from core.timelapse_capture_tenancy_schema import EXPECTED_COLUMNS, TABLE
from core.schema import bootstrap_database
from core.schema.bootstrap import RAW_REQUIRED_COLUMNS, RAW_REQUIRED_TABLES
from core.schema.migrator import MigrationError

ROOT = Path(__file__).resolve().parents[2]
M = importlib.import_module("core.schema.migrations.033_timelapse_capture_tenancy")


def _ids(engine):
    with engine.connect() as c:
        return {row[0] for row in c.execute(text("SELECT migration_id FROM odin_schema_migrations"))}


def test_inventory_lists_the_table_and_its_exact_columns():
    from modules.system.postgres_backup_service import canonical_tables
    assert TABLE in RAW_REQUIRED_TABLES and TABLE in canonical_tables()
    assert RAW_REQUIRED_COLUMNS[TABLE] == frozenset(EXPECTED_COLUMNS) == frozenset({"operation_id", "captured_single_tenant", "created_at"})


def test_fresh_sqlite_boot_applies_033_once_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        first = bootstrap_database(engine)
        assert first["applied"][-3:] == ["python:032-quiet-hours-deferred-deliveries", M.MIGRATION_ID, "orm-baseline:1"]
        assert {c["name"] for c in inspect(engine).get_columns(TABLE)} == EXPECTED_COLUMNS
        second = bootstrap_database(engine)
        assert second["applied"] == [] and second["schema_fingerprint"] == first["schema_fingerprint"]
    finally:
        engine.dispose()


def _pre033(tmp_path, with_anchor=True):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre033.db'}")
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text(f"DROP TABLE {TABLE}"))
        c.execute(text("DELETE FROM odin_schema_migrations WHERE migration_id=:id"), {"id": M.MIGRATION_ID})
        if with_anchor:  # an existing (pre-033) capture: it keeps no row and uses the fallback
            c.execute(text("INSERT INTO printers(id,name) VALUES(1,'P')"))
            c.execute(text("INSERT INTO timelapses(id,printer_id,filename,status,ownership_operation_id) VALUES(1,1,'a.mp4','ready','" + "a" * 32 + "')"))
            c.execute(text("INSERT INTO timelapse_ownership_anchors(operation_id,timelapse_id,printer_id,filename,owner_state,ownership_source,"
                           "capture_printer_shared) VALUES(:n,1,1,'a.mp4','unresolved','capture_unresolved',FALSE)"), {"n": "a" * 32})
    return engine


def test_pre033_database_upgrades_and_keeps_its_rows_without_backfill(tmp_path):
    engine = _pre033(tmp_path)
    try:
        result = bootstrap_database(engine)
        assert result["applied"] == [M.MIGRATION_ID] and M.MIGRATION_ID in _ids(engine)
        with engine.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM timelapse_ownership_anchors")).scalar_one() == 1
            assert c.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 0  # fallback, never inferred
    finally:
        engine.dispose()


@pytest.mark.parametrize("ddl", [
    f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) PRIMARY KEY)",
    f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) PRIMARY KEY, captured_single_tenant BOOLEAN, created_at DATETIME NOT NULL)",
    f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) PRIMARY KEY, captured_single_tenant BOOLEAN NOT NULL, created_at DATETIME NOT NULL)",
    # Round 5: ondelete, column types and the created_at default are validated too.
    f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) NOT NULL PRIMARY KEY REFERENCES timelapse_ownership_anchors(operation_id), "
    "captured_single_tenant BOOLEAN NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) NOT NULL PRIMARY KEY REFERENCES timelapse_ownership_anchors(operation_id) ON DELETE SET NULL, "
    "captured_single_tenant BOOLEAN NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    f"CREATE TABLE {TABLE}(operation_id VARCHAR(32) NOT NULL PRIMARY KEY REFERENCES timelapse_ownership_anchors(operation_id) ON DELETE CASCADE, "
    "captured_single_tenant BOOLEAN NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) NOT NULL PRIMARY KEY REFERENCES timelapse_ownership_anchors(operation_id) ON DELETE CASCADE, "
    "captured_single_tenant INTEGER NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) NOT NULL PRIMARY KEY REFERENCES timelapse_ownership_anchors(operation_id) ON DELETE CASCADE, "
    "captured_single_tenant BOOLEAN NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) NOT NULL PRIMARY KEY REFERENCES timelapse_ownership_anchors(operation_id) ON DELETE CASCADE, "
    "captured_single_tenant BOOLEAN NOT NULL, created_at DATETIME NOT NULL)",
    f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) NOT NULL PRIMARY KEY REFERENCES timelapse_ownership_anchors(operation_id) ON DELETE CASCADE, "
    "captured_single_tenant BOOLEAN NOT NULL, created_at DATETIME NOT NULL DEFAULT '2020-01-01')",
])
def test_a_different_existing_table_shape_is_refused(tmp_path, ddl):
    engine = _pre033(tmp_path, with_anchor=False)
    try:
        with engine.begin() as c:
            c.execute(text(ddl))
        with pytest.raises(MigrationError):
            bootstrap_database(engine)
        assert M.MIGRATION_ID not in _ids(engine)
    finally:
        engine.dispose()


def test_the_exact_shape_written_by_hand_is_accepted(tmp_path):
    """Control for the refusals above: the same DDL with every validated property passes."""
    engine = _pre033(tmp_path, with_anchor=False)
    try:
        with engine.begin() as c:
            c.execute(text(f"CREATE TABLE {TABLE}(operation_id VARCHAR(64) NOT NULL PRIMARY KEY REFERENCES "
                           "timelapse_ownership_anchors(operation_id) ON DELETE CASCADE, captured_single_tenant BOOLEAN NOT NULL, "
                           "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"))
        assert M.MIGRATION_ID in bootstrap_database(engine)["applied"]
    finally:
        engine.dispose()


def test_rows_follow_their_anchor(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cascade.db'}")
    try:
        bootstrap_database(engine)
        with engine.begin() as c:
            c.execute(text("PRAGMA foreign_keys=ON"))
            c.execute(text("INSERT INTO printers(id,name) VALUES(1,'P')"))
            c.execute(text("INSERT INTO timelapse_ownership_anchors(operation_id,timelapse_id,printer_id,filename,owner_state,ownership_source,"
                           "capture_printer_shared) VALUES(:n,1,1,'a.mp4','unresolved','capture_unresolved',FALSE)"), {"n": "b" * 32})
            c.execute(text(f"INSERT INTO {TABLE}(operation_id,captured_single_tenant) VALUES(:n,TRUE)"), {"n": "b" * 32})
            with pytest.raises(Exception):
                c.execute(text(f"INSERT INTO {TABLE}(operation_id,captured_single_tenant) VALUES('missing-anchor',TRUE)"))
    finally:
        engine.dispose()


def test_published_v1_9_24_database_upgrades_through_033_with_the_entrypoint_command(tmp_path):
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
    assert M.MIGRATION_ID in first["applied"]
    assert second["applied"] == [] and second["schema_fingerprint"] == first["schema_fingerprint"]
    engine = create_engine(f"sqlite:///{db}")
    try:
        assert {c["name"] for c in inspect(engine).get_columns(TABLE)} == EXPECTED_COLUMNS
    finally:
        engine.dispose()


def test_sqlite_backup_restore_round_trip_keeps_capture_tenancy_rows(tmp_path):
    from modules.system.backup_service import apply_pending_restore, create_online_backup, finalize_pending_restore, stage_restore
    db = tmp_path / "live" / "odin.db"
    db.parent.mkdir()
    url = f"sqlite:///{db}"
    engine = create_engine(url)
    try:
        bootstrap_database(engine)
        with engine.begin() as c:
            c.execute(text("INSERT INTO timelapse_ownership_anchors(operation_id,timelapse_id,printer_id,filename,owner_state,ownership_source,"
                           "capture_printer_shared) VALUES(:n,1,1,'a.mp4','unresolved','capture_unresolved',FALSE)"), {"n": "c" * 32})
            c.execute(text(f"INSERT INTO {TABLE}(operation_id,captured_single_tenant) VALUES(:n,TRUE)"), {"n": "c" * 32})
    finally:
        engine.dispose()
    backup, _ = create_online_backup(url, base_dir=tmp_path / "backups")
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text(f"DELETE FROM {TABLE}"))
    engine.dispose()
    stage_restore(backup, url, base_dir=tmp_path / "backups")
    assert apply_pending_restore(url, base_dir=tmp_path / "backups", pid_file=None) is not None
    engine = create_engine(url)
    try:
        bootstrap_database(engine)  # the restored file boots through current sources
        finalize_pending_restore(url, base_dir=tmp_path / "backups", pid_file=None)
        with engine.connect() as c:
            assert c.execute(text(f"SELECT operation_id,captured_single_tenant FROM {TABLE}")).all() == [("c" * 32, 1)]
    finally:
        engine.dispose()
