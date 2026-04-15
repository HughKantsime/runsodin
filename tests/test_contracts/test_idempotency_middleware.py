"""
Contract test — Idempotency-Key middleware (v1.8.9, post-codex-fix).

Validates the claim-before-execute primitive that backs `queue_job`,
`cancel_job`, `approve_job`, etc. in the MCP tool surface.

Covers:
  1. Classifier on every state (miss, pending, conflict, hit, expired,
     stuck-pending treated as miss).
  2. Claim-before-execute: _try_claim is atomic under concurrent INSERT.
  3. _finalize_row transitions pending → complete.
  4. _release_row drops pending rows on non-2xx.
  5. _reclaim_expired overwrites stuck/expired rows back to pending.
  6. Per-user PK scope preserved.
  7. TTL prune handles both complete and pending states.
  8. Request-hash canonicalization.

Unit-level against the pure helpers. The full middleware coroutine
(call_next wrapping, body streaming) is exercised by the live
integration suite.
"""

import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="SQLAlchemy not installed in test venv")
pytest.importorskip("fastapi", reason="FastAPI not installed in test venv")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

MIGRATION_005 = (
    BACKEND_DIR / "core" / "migrations" / "005_idempotency_keys.sql"
)


def _seed_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MIGRATION_005.read_text(encoding="utf-8"))
    conn.commit()


def _fake_db(conn: sqlite3.Connection):
    """Minimal SQLAlchemy-like session wrapping a sqlite3 conn."""

    class _Result:
        def __init__(self, cur):
            self._cur = cur
            try:
                self.rowcount = cur.rowcount
            except Exception:
                self.rowcount = 0

        def fetchone(self):
            row = self._cur.fetchone()
            if row is None:
                return None
            # Build an attribute-accessible row wrapper so helpers that
            # use .attr access (row.state, row.response_status) work
            # against the raw sqlite3 tuple.
            names = [d[0] for d in self._cur.description]

            class _Row(tuple):
                def __getattr__(self, k):
                    try:
                        return self[names.index(k)]
                    except ValueError:
                        raise AttributeError(k)

            return _Row(row)

        def fetchall(self):
            rows = self._cur.fetchall()
            names = [d[0] for d in self._cur.description]

            class _Row(tuple):
                def __getattr__(self, k):
                    try:
                        return self[names.index(k)]
                    except ValueError:
                        raise AttributeError(k)

            return [_Row(r) for r in rows]

    class _FakeDB:
        def __init__(self, c):
            self.c = c

        def execute(self, clause, params=None):
            sql = str(clause.compile(compile_kwargs={"literal_binds": False}))
            if params:
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


# ---------------------------------------------------------------------------
# Claim-before-execute primitives
# ---------------------------------------------------------------------------

def test_try_claim_succeeds_on_empty_slot(conn):
    from core.middleware.idempotency import _try_claim

    db = _fake_db(conn)
    assert _try_claim(db, "k", 1, "POST", "/x", "h") is True

    row = conn.execute(
        "SELECT state, response_status FROM idempotency_keys WHERE key='k'"
    ).fetchone()
    assert row == ("pending", 0)


def test_try_claim_fails_on_collision(conn):
    from core.middleware.idempotency import _try_claim

    db = _fake_db(conn)
    assert _try_claim(db, "k", 1, "POST", "/x", "h") is True
    # Second attempt on the same (key, user_id) must fail.
    assert _try_claim(db, "k", 1, "POST", "/x", "h") is False


def test_try_claim_same_key_different_user_both_succeed(conn):
    from core.middleware.idempotency import _try_claim

    db = _fake_db(conn)
    assert _try_claim(db, "k", 1, "POST", "/x", "h") is True
    assert _try_claim(db, "k", 2, "POST", "/x", "h") is True


def test_finalize_row_transitions_pending_to_complete(conn):
    from core.middleware.idempotency import _try_claim, _finalize_row

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "h")
    _finalize_row(db, "k", 1, 201, b'{"id": 7}')

    row = conn.execute(
        "SELECT state, response_status, response_body "
        "FROM idempotency_keys WHERE key='k' AND user_id=1"
    ).fetchone()
    assert row == ("complete", 201, '{"id": 7}')


def test_release_row_drops_pending(conn):
    from core.middleware.idempotency import _try_claim, _release_row

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "h")
    _release_row(db, "k", 1)

    count = conn.execute(
        "SELECT COUNT(*) FROM idempotency_keys WHERE key='k'"
    ).fetchone()[0]
    assert count == 0


def test_release_row_leaves_completed_intact(conn):
    from core.middleware.idempotency import _try_claim, _finalize_row, _release_row

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "h")
    _finalize_row(db, "k", 1, 200, b"{}")
    _release_row(db, "k", 1)  # should be a no-op for completed rows

    row = conn.execute(
        "SELECT state FROM idempotency_keys WHERE key='k'"
    ).fetchone()
    assert row == ("complete",)


def test_reclaim_expired_cas_single_winner(conn):
    """Reclaim is a compare-and-set on prior_created_at: exactly one
    concurrent reclaimer succeeds, the other sees rowcount=0."""
    from core.middleware.idempotency import (
        _try_claim, _finalize_row, _lookup_row, _reclaim_expired,
    )

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "oldhash")
    _finalize_row(db, "k", 1, 200, b"{}")
    # Read the current created_at — both racers would see this value.
    _, _, _, prior = _lookup_row(db, "k", 1, "oldhash")
    assert prior is not None

    # First reclaimer wins.
    assert _reclaim_expired(
        db, "k", 1, "POST", "/x", "newhash", prior_created_at=prior
    ) is True

    # Second reclaimer passes the SAME prior_created_at, but the row's
    # created_at has been bumped by the winner → 0 rows affected.
    assert _reclaim_expired(
        db, "k", 1, "POST", "/x", "othernewhash", prior_created_at=prior
    ) is False

    # State reflects the winner.
    row = conn.execute(
        "SELECT state, request_hash FROM idempotency_keys WHERE key='k'"
    ).fetchone()
    assert row == ("pending", "newhash")


def test_finalize_row_raises_on_missing_row(conn):
    """A finalize against a deleted/missing pending row raises
    IdempotencyFinalizeError (codex pass 2: fail loud)."""
    from core.middleware.idempotency import (
        _finalize_row, IdempotencyFinalizeError,
    )

    db = _fake_db(conn)
    with pytest.raises(IdempotencyFinalizeError):
        _finalize_row(db, "nonexistent", 1, 200, b"{}")


def test_finalize_row_raises_on_non_pending_state(conn):
    """Finalizing a row that's already 'complete' is a rowcount=0
    event → raise."""
    from core.middleware.idempotency import (
        _try_claim, _finalize_row, IdempotencyFinalizeError,
    )

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "h")
    _finalize_row(db, "k", 1, 200, b"{}")  # first finalize ok
    with pytest.raises(IdempotencyFinalizeError):
        _finalize_row(db, "k", 1, 200, b"{}")  # second must raise


# ---------------------------------------------------------------------------
# Lookup classifier
# ---------------------------------------------------------------------------

def test_lookup_classifies_miss(conn):
    from core.middleware.idempotency import _lookup_row, _LOOKUP_MISS

    db = _fake_db(conn)
    cls, _, _, _ = _lookup_row(db, "missing", 1, "h")
    assert cls == _LOOKUP_MISS


def test_lookup_classifies_pending_same_hash(conn):
    from core.middleware.idempotency import (
        _try_claim, _lookup_row, _LOOKUP_PENDING,
    )

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "h")
    cls, _, _, _ = _lookup_row(db, "k", 1, "h")
    assert cls == _LOOKUP_PENDING


def test_lookup_classifies_conflict_when_pending_with_different_hash(conn):
    from core.middleware.idempotency import (
        _try_claim, _lookup_row, _LOOKUP_CONFLICT,
    )

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "original")
    cls, _, _, _ = _lookup_row(db, "k", 1, "different")
    assert cls == _LOOKUP_CONFLICT


def test_lookup_classifies_hit(conn):
    from core.middleware.idempotency import (
        _try_claim, _finalize_row, _lookup_row, _LOOKUP_HIT,
    )

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "h")
    _finalize_row(db, "k", 1, 201, b'{"id": 9}')
    cls, status, body, _ = _lookup_row(db, "k", 1, "h")
    assert cls == _LOOKUP_HIT
    assert status == 201
    assert body == '{"id": 9}'


def test_lookup_classifies_conflict_on_complete_with_different_hash(conn):
    from core.middleware.idempotency import (
        _try_claim, _finalize_row, _lookup_row, _LOOKUP_CONFLICT,
    )

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "h")
    _finalize_row(db, "k", 1, 200, b"{}")
    cls, _, _, _ = _lookup_row(db, "k", 1, "different")
    assert cls == _LOOKUP_CONFLICT


def test_lookup_classifies_expired(conn):
    from core.middleware.idempotency import _lookup_row, _LOOKUP_EXPIRED

    past = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO idempotency_keys
        (key, user_id, method, path, request_hash, state,
         response_status, response_body, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("old", 1, "POST", "/x", "h", "complete", 200, "{}", past, now),
    )
    conn.commit()

    db = _fake_db(conn)
    cls, _, _, created_at_str = _lookup_row(db, "old", 1, "h")
    assert cls == _LOOKUP_EXPIRED
    assert created_at_str == past


def test_stuck_pending_row_classifies_as_stuck_pending(conn):
    """A pending row older than the 90s watchdog is treated as
    stuck_pending so the caller can CAS-claim it (single winner)."""
    from core.middleware.idempotency import _lookup_row, _LOOKUP_STUCK_PENDING

    past = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO idempotency_keys
        (key, user_id, method, path, request_hash, state,
         response_status, response_body, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("stuck", 1, "POST", "/x", "h", "pending", 0, "", past, now),
    )
    conn.commit()

    db = _fake_db(conn)
    cls, _, _, created_at_str = _lookup_row(db, "stuck", 1, "h")
    assert cls == _LOOKUP_STUCK_PENDING
    assert created_at_str == past


# ---------------------------------------------------------------------------
# Request-hash helper
# ---------------------------------------------------------------------------

def test_request_hash_canonicalizes_json_key_order():
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/q", b'{"a":1,"b":2}')
    h2 = _compute_request_hash("POST", "/q", b'{"b":2,"a":1}')
    assert h1 == h2


def test_request_hash_distinguishes_different_bodies():
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/q", b'{"model_id":1}')
    h2 = _compute_request_hash("POST", "/q", b'{"model_id":2}')
    assert h1 != h2


def test_request_hash_distinguishes_method_and_path():
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/a", b"{}")
    h2 = _compute_request_hash("POST", "/b", b"{}")
    h3 = _compute_request_hash("PUT", "/a", b"{}")
    assert h1 != h2
    assert h1 != h3
    assert h2 != h3


def test_request_hash_handles_non_json_body():
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/u", b"\xff\xfe\xfd")
    h2 = _compute_request_hash("POST", "/u", b"\xff\xfe\xfd")
    h3 = _compute_request_hash("POST", "/u", b"\xff\xfe\xfc")
    assert h1 == h2
    assert h1 != h3


def test_request_hash_includes_query_string():
    """Codex pass 2: identical body but different query = different hash.

    Motivation: `POST /vision/models?name=X` vs
    `POST /vision/models?name=Y` with the same upload body would
    otherwise replay each other's result.
    """
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/vision/models", b"BODY", query="name=foo")
    h2 = _compute_request_hash("POST", "/vision/models", b"BODY", query="name=bar")
    h3 = _compute_request_hash("POST", "/vision/models", b"BODY", query="")
    assert h1 != h2
    assert h1 != h3
    assert h2 != h3


def test_request_hash_query_string_order_canonicalized():
    """?a=1&b=2 and ?b=2&a=1 hash the same."""
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/x", b"{}", query="a=1&b=2")
    h2 = _compute_request_hash("POST", "/x", b"{}", query="b=2&a=1")
    assert h1 == h2


def test_request_hash_query_string_preserves_repeated_keys():
    """?tag=a&tag=b vs ?tag=a → different hash (different semantics)."""
    from core.middleware.idempotency import _compute_request_hash

    h1 = _compute_request_hash("POST", "/x", b"{}", query="tag=a&tag=b")
    h2 = _compute_request_hash("POST", "/x", b"{}", query="tag=a")
    assert h1 != h2


# ---------------------------------------------------------------------------
# Prune
# ---------------------------------------------------------------------------

def test_prune_removes_expired_complete_and_stuck_pending(conn):
    from core.middleware.idempotency import prune_expired_idempotency_keys

    now = datetime.now(timezone.utc)
    long_ago = (now - timedelta(hours=30)).isoformat()        # expired complete
    stuck = (now - timedelta(minutes=5)).isoformat()          # stuck pending
    fresh = now.isoformat()                                    # still valid

    cases = [
        ("k1", "complete", long_ago),
        ("k2", "pending",  stuck),
        ("k3", "complete", fresh),
        ("k4", "pending",  fresh),
    ]
    for key, state, ts in cases:
        conn.execute(
            """
            INSERT INTO idempotency_keys
            (key, user_id, method, path, request_hash, state,
             response_status, response_body, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key, 1, "POST", "/x", "h", state, 0, "", ts, ts),
        )
    conn.commit()

    db = _fake_db(conn)
    deleted = prune_expired_idempotency_keys(db)
    assert deleted == 2

    remaining = {
        r[0]
        for r in conn.execute(
            "SELECT key FROM idempotency_keys"
        ).fetchall()
    }
    assert remaining == {"k3", "k4"}


def test_prune_does_not_delete_fresh_rows_on_mixed_timestamp_formats(conn):
    """Codex pass 2 regression guard: if a row were written with
    SQLite's CURRENT_TIMESTAMP format ('YYYY-MM-DD HH:MM:SS') instead
    of ISO-8601 ('YYYY-MM-DDTHH:MM:SS+00:00'), lexical comparison
    against an ISO cutoff would false-expire it.

    The schema no longer defaults to CURRENT_TIMESTAMP — _try_claim
    writes explicit ISO — so a fresh claim stays safe from prune.
    """
    from core.middleware.idempotency import (
        _try_claim, prune_expired_idempotency_keys,
    )

    db = _fake_db(conn)
    assert _try_claim(db, "fresh", 1, "POST", "/x", "h") is True

    deleted = prune_expired_idempotency_keys(db)
    assert deleted == 0

    remaining = conn.execute(
        "SELECT COUNT(*) FROM idempotency_keys"
    ).fetchone()[0]
    assert remaining == 1


# ---------------------------------------------------------------------------
# _resolve_user_id — auth-tight version (codex fix 2)
# ---------------------------------------------------------------------------

def test_resolve_user_id_rejects_inactive_user(conn):
    """Even with a valid token match, is_active=0 → None (no replay)."""
    from core.middleware.idempotency import _resolve_user_id
    from types import SimpleNamespace

    # Seed a users row with is_active=0 and a token for that user.
    # Requires the users + api_tokens tables — seed just enough schema.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            token_prefix TEXT,
            token_hash TEXT,
            expires_at TEXT
        );
        INSERT INTO users (id, username, is_active) VALUES (1, 'u', 0);
        """
    )
    conn.commit()

    # Build a fake request with an Authorization Bearer. The JWT
    # decode path hits `decode_access_token`; if that raises, the
    # function falls through to None — same end result for the test.
    class _Req:
        class _H:
            _data = {"Authorization": "Bearer garbage"}
            def get(self, k, default=None):
                return self._data.get(k, default)
        headers = _H()

    db = _fake_db(conn)
    assert _resolve_user_id(_Req(), db) is None
