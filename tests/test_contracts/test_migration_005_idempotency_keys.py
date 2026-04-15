"""
Contract test — Migration 005: idempotency_keys table.

Verifies that `backend/core/migrations/005_idempotency_keys.sql` applies
cleanly to both a fresh SQLite database and a second (re-applied) run,
and that the resulting table + index have the expected schema.

This migration is part of v1.8.9's agent-native surface. The table backs
the Idempotency-Key middleware that caches write-endpoint responses for
24h keyed on (key, user_id).

Run: pytest tests/test_contracts/test_migration_005_idempotency_keys.py -v
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "migrations"
    / "005_idempotency_keys.sql"
)

EXPECTED_COLUMNS = {
    "key",
    "user_id",
    "method",
    "path",
    "request_hash",
    "state",             # added post-codex: 'pending' or 'complete'
    "response_status",
    "response_body",
    "created_at",
    "updated_at",        # added post-codex: tracks state transitions
}


def _apply_migration(conn: sqlite3.Connection) -> None:
    """Apply migration 005 to a SQLite connection via executescript."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()


@pytest.fixture
def fresh_db():
    """Provide a fresh in-memory SQLite DB with no migrations applied."""
    conn = sqlite3.connect(":memory:")
    try:
        yield conn
    finally:
        conn.close()


def test_migration_file_exists():
    """Migration file is present at the expected path."""
    assert MIGRATION_PATH.exists(), f"Migration missing: {MIGRATION_PATH}"


def test_migration_applies_cleanly(fresh_db):
    """Fresh install: migration runs without raising."""
    _apply_migration(fresh_db)

    # Table exists
    rows = fresh_db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='idempotency_keys'"
    ).fetchall()
    assert rows, "idempotency_keys table not created"


def test_migration_columns_match_spec(fresh_db):
    """All expected columns are present with correct names."""
    _apply_migration(fresh_db)

    cols = {
        row[1]  # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
        for row in fresh_db.execute("PRAGMA table_info(idempotency_keys)").fetchall()
    }
    assert cols == EXPECTED_COLUMNS, (
        f"Column mismatch.\n  Got: {sorted(cols)}\n  Want: {sorted(EXPECTED_COLUMNS)}"
    )


def test_migration_primary_key_is_composite(fresh_db):
    """PK is (key, user_id) — enforces per-user scoping of idempotency keys."""
    _apply_migration(fresh_db)

    pk_cols = [
        row[1]
        for row in fresh_db.execute("PRAGMA table_info(idempotency_keys)").fetchall()
        if row[5] > 0  # pk column index > 0
    ]
    assert set(pk_cols) == {"key", "user_id"}, (
        f"PK columns expected to be {{key, user_id}}, got {pk_cols}"
    )


def test_migration_created_at_index_exists(fresh_db):
    """TTL-prune index on created_at is present."""
    _apply_migration(fresh_db)

    rows = fresh_db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='ix_idempotency_keys_created_at'"
    ).fetchall()
    assert rows, "ix_idempotency_keys_created_at index not created"


def test_migration_is_idempotent(fresh_db):
    """Re-applying the migration does not raise (IF NOT EXISTS guards)."""
    _apply_migration(fresh_db)
    _apply_migration(fresh_db)  # Second apply should be a no-op.

    # Still exactly one table + one index.
    tables = fresh_db.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='idempotency_keys'"
    ).fetchone()[0]
    assert tables == 1

    indexes = fresh_db.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='index' AND name='ix_idempotency_keys_created_at'"
    ).fetchone()[0]
    assert indexes == 1


def test_migration_insert_and_replay_roundtrip(fresh_db):
    """End-to-end smoke: insert a completed row, query it back by (key, user_id)."""
    _apply_migration(fresh_db)

    fresh_db.execute(
        """
        INSERT INTO idempotency_keys
        (key, user_id, method, path, request_hash, state,
         response_status, response_body)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "11111111-2222-3333-4444-555555555555",
            42,
            "POST",
            "/api/v1/jobs",
            "abc123",
            "complete",
            200,
            '{"job_id": 7}',
        ),
    )
    fresh_db.commit()

    row = fresh_db.execute(
        "SELECT state, response_status, response_body "
        "FROM idempotency_keys "
        "WHERE key = ? AND user_id = ?",
        ("11111111-2222-3333-4444-555555555555", 42),
    ).fetchone()
    assert row == ("complete", 200, '{"job_id": 7}')


def test_state_defaults_to_pending(fresh_db):
    """INSERT with only required fields gets state='pending' by default."""
    _apply_migration(fresh_db)

    fresh_db.execute(
        """
        INSERT INTO idempotency_keys
        (key, user_id, method, path, request_hash)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("abc", 1, "POST", "/x", "h"),
    )
    fresh_db.commit()

    state = fresh_db.execute(
        "SELECT state, response_status, response_body "
        "FROM idempotency_keys WHERE key = 'abc'"
    ).fetchone()
    assert state == ("pending", 0, "")


def test_state_index_exists(fresh_db):
    """ix_idempotency_keys_state index is present for state-based scans."""
    _apply_migration(fresh_db)

    rows = fresh_db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='ix_idempotency_keys_state'"
    ).fetchall()
    assert rows


def test_same_key_different_users_do_not_collide(fresh_db):
    """PK scope: same key can exist for user A and user B independently."""
    _apply_migration(fresh_db)

    key = "88888888-7777-6666-5555-444444444444"
    for user_id, body in [(1, '{"a": 1}'), (2, '{"b": 2}')]:
        fresh_db.execute(
            """
            INSERT INTO idempotency_keys
            (key, user_id, method, path, request_hash, state,
             response_status, response_body)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key, user_id, "POST", "/x", "h", "complete", 200, body),
        )
    fresh_db.commit()

    count = fresh_db.execute(
        "SELECT COUNT(*) FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()[0]
    assert count == 2
