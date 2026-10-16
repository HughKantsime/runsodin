"""Migration 032 (first registered post-027 migration): quiet-hours deferred deliveries.

Fresh boot, upgrade of an earlier database and of the published v1.9.24 database,
exact-shape refusal, backup inventory, and retention. User/org deletion cleanup is
test_cert_E_deferred_cleanup.py; the PostgreSQL proof is run_post027_restore.py.
"""
from datetime import datetime, timedelta, timezone
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

from core.quiet_hours_deferral_schema import EXPECTED_COLUMNS, TABLE
from core.schema import bootstrap_database
from core.schema.bootstrap import RAW_REQUIRED_COLUMNS, RAW_REQUIRED_TABLES
from core.schema.migrator import MigrationError

ROOT = Path(__file__).resolve().parents[2]
M = importlib.import_module("core.schema.migrations.032_quiet_hours_deferred_deliveries")


def _ids(engine):
    with engine.connect() as c:
        return {row[0] for row in c.execute(text("SELECT migration_id FROM odin_schema_migrations"))}


def _insert(c, row_id, state="pending", created=None, user_id=3):
    c.execute(text(f"INSERT INTO {TABLE}(id,user_id,org_id,scope_key,channel,alert_type,severity,title,state,created_at) "
                   "VALUES(:i,:u,1,'org:1','email','print_failed','critical','t',:s,:c)"),
              {"i": row_id, "u": user_id, "s": state, "c": created or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})


def test_inventory_lists_the_table_and_its_exact_columns():
    from modules.system.postgres_backup_service import canonical_tables
    assert TABLE in RAW_REQUIRED_TABLES and TABLE in canonical_tables()
    assert RAW_REQUIRED_COLUMNS[TABLE] == frozenset(EXPECTED_COLUMNS)


def test_fresh_sqlite_boot_applies_032_once_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        first = bootstrap_database(engine)
        # Later post-027 migrations (033...) follow 032; the ORM baseline is recorded last.
        assert M.MIGRATION_ID in first["applied"] and first["applied"][-1] == "orm-baseline:1"
        assert first["applied"].index(M.MIGRATION_ID) < first["applied"].index("orm-baseline:1")
        assert {c["name"] for c in inspect(engine).get_columns(TABLE)} == EXPECTED_COLUMNS
        second = bootstrap_database(engine)
        assert second["applied"] == [] and second["schema_fingerprint"] == first["schema_fingerprint"]
    finally:
        engine.dispose()


def _pre032(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pre032.db'}")
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text(f"DROP TABLE {TABLE}"))
        c.execute(text("DELETE FROM odin_schema_migrations WHERE migration_id=:id"), {"id": M.MIGRATION_ID})
        c.execute(text("INSERT INTO system_config(key,value) VALUES('marker','\"kept\"')"))
    return engine


def test_pre032_database_upgrades_and_keeps_its_rows(tmp_path):
    engine = _pre032(tmp_path)
    try:
        result = bootstrap_database(engine)
        assert result["applied"] == [M.MIGRATION_ID] and M.MIGRATION_ID in _ids(engine)
        with engine.connect() as c:
            assert c.execute(text("SELECT value FROM system_config WHERE key='marker'")).scalar_one() in ('"kept"', "kept")
    finally:
        engine.dispose()


@pytest.mark.parametrize("ddl", [
    f"CREATE TABLE {TABLE}(id VARCHAR(32) PRIMARY KEY, user_id INTEGER)",
    f"CREATE TABLE {TABLE}(id VARCHAR(32) PRIMARY KEY, user_id INTEGER, org_id INTEGER, scope_key VARCHAR(40), channel VARCHAR(10) NOT NULL, "
    "alert_type VARCHAR(64) NOT NULL, severity VARCHAR(20) NOT NULL, title TEXT NOT NULL, message TEXT, printer_id INTEGER, job_id INTEGER, "
    "spool_id INTEGER, state VARCHAR(10) NOT NULL DEFAULT 'pending', outcome_reason TEXT, created_at DATETIME NOT NULL, delivered_at DATETIME)",
])
def test_a_different_existing_table_shape_is_refused(tmp_path, ddl):
    engine = _pre032(tmp_path)
    try:
        with engine.begin() as c:
            c.execute(text(ddl))
        with pytest.raises(MigrationError):
            bootstrap_database(engine)
        assert M.MIGRATION_ID not in _ids(engine)
    finally:
        engine.dispose()


def test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command(tmp_path):
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


def test_retention_purges_sent_after_7_days_and_unsent_after_30(tmp_path):
    from modules.notifications import deferred_deliveries
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.db'}")
    try:
        bootstrap_database(engine)
        now = datetime.now(timezone.utc)
        stamp = lambda days: (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with engine.begin() as c:
            _insert(c, "sent-old", "sent", stamp(8)); _insert(c, "sent-new", "sent", stamp(6))
            _insert(c, "failed-old", "failed", stamp(31)); _insert(c, "pending-old", "pending", stamp(31))
            _insert(c, "pending-new", "pending", stamp(29))
        with engine.begin() as c:
            assert deferred_deliveries.purge(c, now=now) == 3
        with engine.connect() as c:
            assert sorted(c.execute(text(f"SELECT id FROM {TABLE}")).scalars()) == ["pending-new", "sent-new"]
    finally:
        engine.dispose()
