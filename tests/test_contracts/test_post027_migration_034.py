"""Migration 034: alert outbox (additive side table).

Fresh boot, upgrade of an earlier database and of the published v1.9.24 database,
exact-shape refusal, foreign-key behaviour and backup inventory. Rows are transient
(deleted when dispatched), follow their job (ON DELETE CASCADE) and lose an erased
actor (ON DELETE SET NULL). The PostgreSQL proof is run_post027_restore.py.
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

from core.schema import bootstrap_database
from core.schema.bootstrap import POST027_MIGRATIONS, RAW_REQUIRED_COLUMNS, RAW_REQUIRED_TABLES
from core.schema.migrator import MigrationError

ROOT = Path(__file__).resolve().parents[2]
M = importlib.import_module("core.schema.migrations.034_alert_outbox")
TABLE, EXPECTED_COLUMNS = M.TABLE, M.EXPECTED_COLUMNS
_GOOD = (f"CREATE TABLE {TABLE}(id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
         "job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, alert_kind VARCHAR(32) NOT NULL, "
         "actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, attempts INTEGER NOT NULL DEFAULT 0, "
         "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)")


def _ids(engine):
    with engine.connect() as c:
        return {row[0] for row in c.execute(text("SELECT migration_id FROM odin_schema_migrations"))}


def test_registered_and_inventoried_with_exact_columns():
    from modules.system.postgres_backup_service import canonical_tables
    assert (M.MIGRATION_ID, "core.schema.migrations.034_alert_outbox") in POST027_MIGRATIONS
    assert TABLE in RAW_REQUIRED_TABLES and TABLE in canonical_tables()
    assert RAW_REQUIRED_COLUMNS[TABLE] == EXPECTED_COLUMNS == frozenset(
        {"id", "job_id", "alert_kind", "actor_user_id", "attempts", "created_at"})
    ledger = json.loads((ROOT / "backend/core/schema/released_post027_ledgers.json").read_text())
    assert ledger["releases"]["2.0.0"][-1] == M.MIGRATION_ID


def test_fresh_sqlite_boot_applies_034_once_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        first = bootstrap_database(engine)
        assert M.MIGRATION_ID in first["applied"] and first["applied"][-1] == "orm-baseline:1"
        assert {c["name"] for c in inspect(engine).get_columns(TABLE)} == EXPECTED_COLUMNS
        second = bootstrap_database(engine)
        assert second["applied"] == [] and second["schema_fingerprint"] == first["schema_fingerprint"]
    finally:
        engine.dispose()


def _pre034(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre034.db'}")
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text(f"DROP TABLE {TABLE}"))
        c.execute(text("DELETE FROM odin_schema_migrations WHERE migration_id=:id"), {"id": M.MIGRATION_ID})
        c.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(1,'existing','pending')"))
    return engine


def test_pre034_database_upgrades_and_keeps_its_rows(tmp_path):
    engine = _pre034(tmp_path)
    try:
        assert bootstrap_database(engine)["applied"] == [M.MIGRATION_ID] and M.MIGRATION_ID in _ids(engine)
        with engine.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one() == 1
            assert c.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() == 0  # no backfill
    finally:
        engine.dispose()


@pytest.mark.parametrize("change", [
    ("alert_kind VARCHAR(32) NOT NULL", "alert_kind VARCHAR(64) NOT NULL"),
    ("alert_kind VARCHAR(32) NOT NULL", "alert_kind VARCHAR(32)"),
    ("actor_user_id INTEGER REFERENCES", "actor_user_id INTEGER NOT NULL REFERENCES"),
    ("ON DELETE CASCADE", "ON DELETE SET NULL"),
    ("ON DELETE SET NULL, attempts", "ON DELETE CASCADE, attempts"),
    ("attempts INTEGER NOT NULL DEFAULT 0", "attempts INTEGER NOT NULL DEFAULT 1"),
    ("attempts INTEGER NOT NULL DEFAULT 0", "attempts TEXT NOT NULL DEFAULT 0"),
    ("DEFAULT CURRENT_TIMESTAMP", "DEFAULT '2020-01-01'"),
    ("REFERENCES jobs(id)", "REFERENCES printers(id)"),
    (", created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP", ""),
])
def test_a_different_existing_table_shape_is_refused(tmp_path, change):
    engine = _pre034(tmp_path)
    try:
        with engine.begin() as c:
            c.execute(text(_GOOD.replace(*change)))
        with pytest.raises(MigrationError):
            bootstrap_database(engine)
        assert M.MIGRATION_ID not in _ids(engine)
    finally:
        engine.dispose()


def test_the_exact_shape_written_by_hand_is_accepted(tmp_path):
    """Control for the refusals above."""
    engine = _pre034(tmp_path)
    try:
        with engine.begin() as c:
            c.execute(text(_GOOD))
        assert M.MIGRATION_ID in bootstrap_database(engine)["applied"]
    finally:
        engine.dispose()


def test_rows_follow_their_job_and_lose_an_erased_actor(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fk.db'}")
    try:
        bootstrap_database(engine)
        with engine.begin() as c:
            c.execute(text("PRAGMA foreign_keys=ON"))
            c.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(7,'u','u@x.test','f','viewer',1)"))
            c.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(1,'a','pending'),(2,'b','pending')"))
            c.execute(text(f"INSERT INTO {TABLE}(job_id,alert_kind,actor_user_id) VALUES(1,'queue_added',7),(2,'queue_added',7)"))
            c.execute(text("DELETE FROM users WHERE id=7"))
            c.execute(text("DELETE FROM jobs WHERE id=1"))
            assert c.execute(text(f"SELECT job_id,actor_user_id,attempts FROM {TABLE}")).all() == [(2, None, 0)]
            with pytest.raises(Exception):
                c.execute(text(f"INSERT INTO {TABLE}(job_id,alert_kind) VALUES(999,'queue_added')"))
    finally:
        engine.dispose()


def test_published_v1_9_24_database_upgrades_through_034_with_the_entrypoint_command(tmp_path):
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


def test_sqlite_backup_restore_round_trip_keeps_outbox_rows(tmp_path):
    from modules.system.backup_service import apply_pending_restore, create_online_backup, finalize_pending_restore, stage_restore
    db = tmp_path / "live" / "odin.db"
    db.parent.mkdir()
    url = f"sqlite:///{db}"
    engine = create_engine(url)
    try:
        bootstrap_database(engine)
        with engine.begin() as c:
            c.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(5,'kept','pending')"))
            c.execute(text(f"INSERT INTO {TABLE}(job_id,alert_kind) VALUES(5,'queue_added')"))
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
        bootstrap_database(engine)
        finalize_pending_restore(url, base_dir=tmp_path / "backups", pid_file=None)
        with engine.connect() as c:
            assert c.execute(text(f"SELECT job_id,alert_kind,attempts FROM {TABLE}")).all() == [(5, "queue_added", 0)]
    finally:
        engine.dispose()
