"""Run every synthetic legacy schema through the exact candidate on PostgreSQL."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import inspect, text

from core.database_config import create_database_engine
from core.schema import bootstrap_database
from tests.fixtures.database_legacy import FIXTURE_NAMES, assert_fixture, build_fixture


DATABASE_URL = os.environ["DATABASE_URL"]
PASSWORD_FILE = os.environ["DATABASE_PASSWORD_FILE"]
BACKEND_ROOT = Path("/app/backend")


def reset_public_schema(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")


def run() -> None:
    engine = create_database_engine(
        DATABASE_URL, role="bootstrap", password_file=PASSWORD_FILE
    )
    try:
        for fixture_name in FIXTURE_NAMES:
            reset_public_schema(engine)
            built = build_fixture(engine, BACKEND_ROOT, fixture_name)
            first = bootstrap_database(engine, BACKEND_ROOT)
            assert_fixture(engine, fixture_name)
            with engine.connect() as connection:
                ledger_before = connection.execute(
                    text(
                        "SELECT migration_id, checksum FROM odin_schema_migrations "
                        "ORDER BY migration_id"
                    )
                ).all()
            second = bootstrap_database(engine, BACKEND_ROOT)
            assert second["applied"] == []
            assert second["schema_fingerprint"] == first["schema_fingerprint"]
            assert_fixture(engine, fixture_name)
            with engine.connect() as connection:
                assert connection.execute(
                    text(
                        "SELECT migration_id, checksum FROM odin_schema_migrations "
                        "ORDER BY migration_id"
                    )
                ).all() == ledger_before
            if fixture_name == "current_noop":
                assert first["applied"] == []
                assert first["schema_fingerprint"] == built["before"][
                    "schema_fingerprint"
                ]

        reset_public_schema(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE users ("
                "id BIGSERIAL PRIMARY KEY, username TEXT, password_hash TEXT)"
            )
        try:
            bootstrap_database(engine, BACKEND_ROOT)
            raise AssertionError("malformed PostgreSQL legacy schema was accepted")
        except RuntimeError as exc:
            assert "Schema validation missing columns on users" in str(exc)
        if "odin_schema_migrations" in inspect(engine).get_table_names():
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT COUNT(*) FROM odin_schema_migrations")
                ).scalar_one() == 0

        reset_public_schema(engine)
        bootstrap_database(engine, BACKEND_ROOT)
    finally:
        engine.dispose()
    print(
        f"postgres-legacy-upgrades: PASS fixtures={len(FIXTURE_NAMES)} "
        "second-run-idempotent malformed-fail-closed"
    )


if __name__ == "__main__":
    run()
