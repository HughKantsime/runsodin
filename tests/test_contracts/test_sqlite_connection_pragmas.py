"""Every SQLite connection gets its PRAGMAs from one place: the engine's connect hook.

journal_mode, busy_timeout and foreign_keys used to be set partly by a one-off
startup connection in core.db (busy_timeout there applied to that connection
only; others got sqlite3's implicit 5 s). create_database_engine now applies all
three on every new DBAPI connection, ORM and raw alike, for every role.
"""
import sqlite3

import pytest


def _pragmas(dbapi):
    cursor = dbapi.cursor()
    try:
        return tuple(cursor.execute(f"PRAGMA {name}").fetchone()[0]
                     for name in ("journal_mode", "busy_timeout", "foreign_keys"))
    finally:
        cursor.close()


@pytest.mark.parametrize("role", ["api", "bootstrap", "worker"])
def test_every_new_connection_is_configured_explicitly(tmp_path, role):
    from core.database_config import SQLITE_BUSY_TIMEOUT_MS, create_database_engine
    engine = create_database_engine(f"sqlite:///{tmp_path / 'p.db'}", role=role)
    seen = []
    for _ in range(3):  # NullPool: each is a fresh DBAPI connection
        with engine.connect() as connection:
            seen.append(_pragmas(connection.connection.dbapi_connection))
    raw = engine.raw_connection()
    try:
        seen.append(_pragmas(raw.driver_connection))
    finally:
        raw.close()
    assert set(seen) == {("wal", SQLITE_BUSY_TIMEOUT_MS, 1)}


def test_busy_timeout_does_not_depend_on_the_driver_default(tmp_path, monkeypatch):
    """Even if sqlite3's implicit connect timeout changed, the hook sets the value."""
    from core import database_config
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real(*a, **{**k, "timeout": 0}))
    engine = database_config.create_database_engine(f"sqlite:///{tmp_path / 'q.db'}")
    with engine.connect() as connection:
        assert _pragmas(connection.connection.dbapi_connection)[1] == database_config.SQLITE_BUSY_TIMEOUT_MS


def test_core_db_has_no_startup_only_pragma_source():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / "backend" / "core" / "db.py").read_text()
    assert "PRAGMA journal_mode" not in source and "PRAGMA busy_timeout" not in source
