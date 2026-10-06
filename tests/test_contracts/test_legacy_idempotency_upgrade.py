from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from core.schema import bootstrap_database
from core.schema.migrator import MigrationError, run_migration_files, split_sql_statements

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
MIGRATION_ID = "core/migrations/006_idempotency_keys_upgrade.sql"
LEGACY_SCHEMA = (ROOT / "tests/fixtures/idempotency-v1912.sql").read_text()


def test_v1912_idempotency_bootstrap_preserves_rows_and_repeats(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        for statement in split_sql_statements(LEGACY_SCHEMA):
            connection.exec_driver_sql(statement)
        for state in ("pending", "complete"):
            connection.execute(text(
                "INSERT INTO idempotency_keys "
                "(key,user_id,method,path,request_hash,state,response_status,response_body,"
                "created_at,updated_at) VALUES (:key,1,'POST','/fixture','hash',:state,"
                ":status,:body,'2026-10-06T22:00:00+00:00','2026-10-06T22:00:00+00:00')"
            ), {"key": state, "state": state, "status": 201 if state == "complete" else 0,
                "body": '{"saved":true}' if state == "complete" else ""})
        before = connection.execute(text("SELECT * FROM idempotency_keys ORDER BY key")).all()
    first = bootstrap_database(engine, BACKEND)
    assert MIGRATION_ID in first["applied"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM idempotency_keys ORDER BY key")).all() == before
        assert next(row for row in connection.exec_driver_sql("PRAGMA table_info(idempotency_keys)")
                    if row[1] == "updated_at")[4] is None
    assert bootstrap_database(engine, BACKEND)["applied"] == []
    engine.dispose()


@pytest.mark.parametrize("definition,migration_id", [
    ("INTEGER NOT NULL", MIGRATION_ID),
    ("TEXT", MIGRATION_ID),
    ("TEXT NOT NULL DEFAULT 'unexpected'", MIGRATION_ID),
    ("TEXT NOT NULL", "unrelated/006.sql"),
])
def test_legacy_timestamp_allowlist_rejects_other_shapes(tmp_path, definition, migration_id):
    engine = create_engine(f"sqlite:///{tmp_path / 'wrong.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE TABLE idempotency_keys (updated_at {definition})")
    migration = tmp_path / "migration.sql"
    migration.write_text("ALTER TABLE idempotency_keys ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';\n")
    with pytest.raises(MigrationError, match="idempotency_keys.updated_at"):
        run_migration_files(engine, [(migration_id, migration)])
    engine.dispose()


def test_legacy_timestamp_does_not_accept_unrelated_default_drift(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'drift.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE idempotency_keys (state TEXT NOT NULL)")
    migration = tmp_path / "migration.sql"
    migration.write_text("ALTER TABLE idempotency_keys ADD COLUMN state TEXT NOT NULL DEFAULT 'pending';\n")
    with pytest.raises(MigrationError, match="idempotency_keys.state"):
        run_migration_files(engine, [(MIGRATION_ID, migration)])
    engine.dispose()
