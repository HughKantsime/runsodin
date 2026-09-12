from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.schema import bootstrap_database  # noqa: E402
from tests.fixtures.database_legacy import (  # noqa: E402
    FIXTURE_NAMES,
    assert_fixture,
    build_fixture,
)


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_sqlite_legacy_fixture_converges_and_preserves_sentinel(
    tmp_path: Path, fixture_name: str
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / (fixture_name + '.db')}")
    built = build_fixture(engine, BACKEND, fixture_name)
    first = bootstrap_database(engine, BACKEND)
    assert_fixture(engine, fixture_name)
    with engine.connect() as connection:
        ledger_before = connection.execute(
            text("SELECT migration_id, checksum FROM odin_schema_migrations ORDER BY migration_id")
        ).all()
    second = bootstrap_database(engine, BACKEND)
    assert second["applied"] == []
    assert second["schema_fingerprint"] == first["schema_fingerprint"]
    assert_fixture(engine, fixture_name)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT migration_id, checksum FROM odin_schema_migrations ORDER BY migration_id")
        ).all() == ledger_before
    if fixture_name == "current_noop":
        assert first["applied"] == []
        assert first["schema_fingerprint"] == built["before"]["schema_fingerprint"]
    engine.dispose()


def test_malformed_unledgered_schema_fails_closed(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'malformed.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT)"
        )
    with pytest.raises(RuntimeError, match="Schema validation missing columns on users"):
        bootstrap_database(engine, BACKEND)
    if "odin_schema_migrations" in inspect(engine).get_table_names():
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM odin_schema_migrations")
            ).scalar_one() == 0
    engine.dispose()
