"""
Contract test — Idempotency-Key middleware (v1.8.9).

Validates the agent-safe-retry primitive that backs `queue_job`,
`cancel_job`, `approve_job`, etc. in the MCP tool surface.

Covers:
  1. Miss: no cache row → request executes fresh, response body hashes
     into the cache.
  2. Hit: same key + same body → returns cached response with
     `X-Idempotent-Replay: true`.
  3. Conflict: same key + different body → 409.
  4. Expiry: row older than TTL is not replayed.
  5. Per-user PK scope: same key different users does not cross-replay.
  6. TTL prune deletes expired rows.

The middleware itself is tested as a unit (not via live HTTP) so that
this file has no backend-server dependency and runs in the contract
suite without needing ODIN running.
"""

import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# The middleware module imports SQLAlchemy eagerly. Skip this whole
# file cleanly on machines without a populated test venv — CI runs
# it with the full deps installed.
pytest.importorskip("sqlalchemy", reason="SQLAlchemy not installed in test venv")
pytest.importorskip("fastapi", reason="FastAPI not installed in test venv")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

MIGRATION_005 = (
    BACKEND_DIR / "core" / "migrations" / "005_idempotency_keys.sql"
)


def _seed_schema(conn: sqlite3.Connection) -> None:
    """Apply migration 005 to the scratch DB."""
    conn.executescript(MIGRATION_005.read_text(encoding="utf-8"))
    conn.commit()


def _fake_db(conn: sqlite3.Connection):
    """Minimal SQLAlchemy-like session that wraps a sqlite3 conn.

    Supports db.execute(text_obj, params), db.commit(), db.rollback(),
    which is all the idempotency helpers use.
    """
    class _Result:
        def __init__(self, cur):
            self._cur = cur
            try:
                self.rowcount = cur.rowcount
            except Exception:
                self.rowcount = 0

        def fetchone(self):
            return self._cur.fetchone()

        def fetchall(self):
            return self._cur.fetchall()

    class _FakeDB:
        def __init__(self, c):
            self.c = c

        def execute(self, clause, params=None):
            # The idempotency helpers pass sqlalchemy.text(...) objects;
            # we render them to strings and translate :name → ? for sqlite.
            sql = str(clause.compile(compile_kwargs={"literal_binds": False}))
            if params:
                # Named binds → positional for sqlite3.
                import re
                names_ordered = re.findall(r":(\w+)", sql)
                sql = re.sub(r":\w+", "?", sql)
                ordered_params = [params[n] for n in names_ordered]
                cur = self.c.execute(sql, ordered_params)
            else:
                cur = self.c.execute(sql)
            return _Result(cur)

        def commit(self):
            self.c.commit()

        def rollback(self):
            self.c.rollback()

    return _FakeDB(conn)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    _seed_schema(c)
    yield c
    c.close()


def test_lookup_cached_returns_none_on_miss(conn):
    """Empty cache → lookup returns None."""
    from core.middleware.idempotency import _lookup_cached

    db = _fake_db(conn)
    result = _lookup_cached(db, "missing-key", user_id=1, request_hash="x")
    assert result is None


def test_store_and_hit_roundtrip(conn):
    """Store a row, then look it up with matching hash → hit."""
    from core.middleware.idempotency import _store_cached, _lookup_cached

    db = _fake_db(conn)
    _store_cached(
        db=db,
        key="abc",
        user_id=1,
        method="POST",
        path="/api/v1/queue/add",
        request_hash="hash-1",
        response_status=201,
        response_body=b'{"job_id": 7}',
    )

    result = _lookup_cached(db, "abc", user_id=1, request_hash="hash-1")
    assert result is not None
    status, body, is_conflict = result
    assert status == 201
    assert body == '{"job_id": 7}'
    assert is_conflict is False


def test_conflict_when_same_key_different_hash(conn):
    """Same key + different hash → (0, "", True) conflict sentinel."""
    from core.middleware.idempotency import _store_cached, _lookup_cached

    db = _fake_db(conn)
    _store_cached(
        db=db,
        key="k",
        user_id=1,
        method="POST",
        path="/x",
        request_hash="original",
        response_status=200,
        response_body=b"{}",
    )

    result = _lookup_cached(db, "k", user_id=1, request_hash="different")
    assert result is not None
    _, _, is_conflict = result
    assert is_conflict is True


def test_same_key_different_users_do_not_collide(conn):
    """User A's cached response is invisible to user B."""
    from core.middleware.idempotency import _store_cached, _lookup_cached

    db = _fake_db(conn)
    _store_cached(
        db=db,
        key="shared-key",
        user_id=1,
        method="POST",
        path="/x",
        request_hash="h1",
        response_status=200,
        response_body=b'{"for": "user_1"}',
    )
    _store_cached(
        db=db,
        key="shared-key",
        user_id=2,
        method="POST",
        path="/x",
        request_hash="h2",
        response_status=200,
        response_body=b'{"for": "user_2"}',
    )

    a = _lookup_cached(db, "shared-key", user_id=1, request_hash="h1")
    b = _lookup_cached(db, "shared-key", user_id=2, request_hash="h2")
    assert a[1] == '{"for": "user_1"}'
    assert b[1] == '{"for": "user_2"}'


def test_expired_row_not_replayed(conn):
    """Row older than TTL → lookup returns None (will execute fresh)."""
    from core.middleware.idempotency import _lookup_cached

    db = _fake_db(conn)
    # Insert with an old created_at.
    past = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    conn.execute(
        """
        INSERT INTO idempotency_keys
        (key, user_id, method, path, request_hash, response_status, response_body, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("old", 1, "POST", "/x", "h", 200, "{}", past),
    )
    conn.commit()

    result = _lookup_cached(db, "old", user_id=1, request_hash="h")
    assert result is None


def test_prune_removes_expired_rows(conn):
    """Hourly prune deletes rows older than TTL, keeps fresh ones."""
    from core.middleware.idempotency import prune_expired_idempotency_keys

    past = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    for i, created in enumerate([past, past, now]):
        conn.execute(
            """
            INSERT INTO idempotency_keys
            (key, user_id, method, path, request_hash, response_status, response_body, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"k{i}", 1, "POST", "/x", "h", 200, "{}", created),
        )
    conn.commit()

    db = _fake_db(conn)
    deleted = prune_expired_idempotency_keys(db)
    remaining = conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0]
    assert deleted == 2
    assert remaining == 1


def test_request_hash_canonicalizes_json_key_order():
    """{a:1,b:2} and {b:2,a:1} hash identically (no spurious 409s)."""
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/q", b'{"a":1,"b":2}')
    h2 = _compute_request_hash("POST", "/q", b'{"b":2,"a":1}')
    assert h1 == h2


def test_request_hash_distinguishes_different_bodies():
    """Different body values → different hash."""
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/q", b'{"model_id":1}')
    h2 = _compute_request_hash("POST", "/q", b'{"model_id":2}')
    assert h1 != h2


def test_request_hash_distinguishes_method_and_path():
    """Path / method differences affect the hash."""
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/a", b"{}")
    h2 = _compute_request_hash("POST", "/b", b"{}")
    h3 = _compute_request_hash("PUT", "/a", b"{}")
    assert h1 != h2
    assert h1 != h3
    assert h2 != h3


def test_request_hash_handles_non_json_body():
    """Binary / non-UTF-8 bodies hash without raising (multipart, etc.)."""
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/u", b"\xff\xfe\xfd")
    h2 = _compute_request_hash("POST", "/u", b"\xff\xfe\xfd")
    h3 = _compute_request_hash("POST", "/u", b"\xff\xfe\xfc")
    assert h1 == h2
    assert h1 != h3
