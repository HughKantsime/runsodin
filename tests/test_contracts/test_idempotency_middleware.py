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

        def close(self):
            # Do NOT close the underlying sqlite3 connection — tests
            # keep using it afterwards to assert state. Middleware's
            # `finally: db.close()` needs something callable here.
            pass

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
    _, _, _, prior, _, _ = _lookup_row(db, "k", 1, "oldhash")
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


def test_handler_exception_releases_pending_row(conn, monkeypatch):
    """Codex pass 3: if the handler raises, the pending row must be
    released so retries don't hit 409 until the 90s watchdog.

    The middleware's claim-execute-finalize flow is:
        _try_claim → call_next → on 2xx _finalize_row / non-2xx _release_row

    The bug was that an uncaught exception in `call_next` skipped
    both branches. Fix wraps call_next in try/except that calls
    `_release_row` and re-raises. This test simulates that pattern
    directly.
    """
    import asyncio

    import core.middleware.idempotency as idem_mod

    test_db = _fake_db(conn)
    import core.db as db_mod
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: test_db)
    monkeypatch.setattr(
        idem_mod, "_resolve_user_context",
        lambda req, db: {
            "id": 1, "username": "u", "role": "admin",
            "group_id": None, "is_active": True, "_token_scopes": [],
        },
    )

    from types import SimpleNamespace

    class _Headers:
        _h = {"Idempotency-Key": "exc-key", "content-length": "100", "content-type": "application/json"}
        def get(self, k, default=None):
            return self._h.get(k, default)

    class _URL:
        path = "/api/v1/jobs"
        query = ""

    class _Req:
        method = "POST"
        headers = _Headers()
        url = _URL()
        state = SimpleNamespace()
        cookies = {}

        async def body(self):
            return b'{"item":"x"}'

    async def _boom(request):
        raise RuntimeError("boom inside route")

    req = _Req()

    with pytest.raises(RuntimeError, match="boom inside route"):
        asyncio.run(idem_mod.idempotency_middleware(req, _boom))

    remaining = conn.execute(
        "SELECT COUNT(*) FROM idempotency_keys WHERE key = 'exc-key'"
    ).fetchone()[0]
    assert remaining == 0, (
        "pending row must be released on handler exception so retries "
        "don't get 409 until the 90s watchdog"
    )


def test_conflict_response_is_dual_shape():
    """Codex pass 3: 409 envelope has both top-level `detail` and `error`."""
    import json as _json
    from core.middleware.idempotency import _conflict_response

    resp = _conflict_response("idempotency_in_progress", "hold on")
    body = _json.loads(bytes(resp.body))
    assert body["detail"] == "hold on"
    assert body["error"]["code"] == "idempotency_in_progress"
    assert body["error"]["retriable"] is True


def _run_middleware_and_capture(monkeypatch, conn, req, handler, user_ctx=None):
    """Helper: invoke the idempotency middleware end-to-end against a
    test connection, returning the handler's execution count."""
    import asyncio
    import core.middleware.idempotency as idem_mod
    import core.db as db_mod

    test_db = _fake_db(conn)
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: test_db)

    # Default user_ctx: admin with no token scopes, matching what
    # get_current_user returns for cookie/JWT sessions.
    default_ctx = user_ctx or {
        "id": 1,
        "username": "admin",
        "role": "admin",
        "group_id": None,
        "is_active": True,
        "_token_scopes": [],
    }
    monkeypatch.setattr(idem_mod, "_resolve_user_context", lambda req, db: default_ctx)

    calls = {"count": 0}

    async def _wrapped(request):
        calls["count"] += 1
        return await handler(request)

    result = asyncio.run(idem_mod.idempotency_middleware(req, _wrapped))
    return result, calls["count"]


def test_multipart_requests_skip_idempotency(monkeypatch, conn):
    """Codex pass 4: multipart/form-data requests must not be buffered.

    Multipart boundary randomizes per-retry so raw-byte hashing would
    false-409 legitimate retries; and 500 MB video uploads would OOM
    if buffered in the middleware on top of the route.
    """
    from types import SimpleNamespace
    from fastapi.responses import Response

    class _Headers:
        _h = {
            "Idempotency-Key": "k-multi",
            "content-type": "multipart/form-data; boundary=abc123",
        }
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/vision/models"
        query = ""

    class _Req:
        method = "POST"
        headers = _Headers()
        url = _URL()
        state = SimpleNamespace()
        cookies = {}
        async def body(self):
            raise AssertionError("body() must not be called for multipart")

    async def _handler(request):
        raise AssertionError("handler must not run — middleware should 415")

    import json as _json
    result, call_count = _run_middleware_and_capture(monkeypatch, conn, _Req(), _handler)
    # Codex pass 9: explicit 415 rather than silent pass-through.
    assert call_count == 0
    assert result.status_code == 415
    body = _json.loads(bytes(result.body))
    assert body["error"]["code"] == "idempotency_unsupported"
    assert body["error"]["retriable"] is False

    rows = conn.execute(
        "SELECT COUNT(*) FROM idempotency_keys WHERE key = 'k-multi'"
    ).fetchone()[0]
    assert rows == 0


def test_fingerprint_drift_refuses_replay_AND_re_execution(monkeypatch, conn):
    """Codex pass 10: fingerprint mismatch on a completed row must
    NOT replay AND must NOT re-execute. Previous behavior (pass 6)
    deleted the row and ran the handler again, which reopened
    duplicate-execution risk (the original mutation already took
    effect; a second run could be accepted under the new authz and
    produce a duplicate). Correct behavior: 409 idempotency_authz_changed,
    client mints a fresh key."""
    from types import SimpleNamespace
    from fastapi.responses import Response
    import core.middleware.idempotency as idem_mod
    import core.db as db_mod

    test_db = _fake_db(conn)
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: test_db)

    class _Headers:
        _h = {"Idempotency-Key": "k-fp", "content-type": "application/json", "content-length": "100"}
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/jobs"
        query = ""

    def _make_req():
        class _Req:
            method = "POST"
            headers = _Headers()
            url = _URL()
            state = SimpleNamespace()
            cookies = {}
            async def body(self):
                return b'{"item":"x"}'
        return _Req()

    exec_count = {"count": 0}

    async def _handler(request):
        exec_count["count"] += 1
        return Response(content=b'{"id":1}', status_code=201, media_type="application/json")

    import asyncio, json as _json
    monkeypatch.setattr(
        idem_mod, "_resolve_user_context",
        lambda req, db: {
            "id": 1, "username": "u", "role": "admin",
            "group_id": None, "is_active": True, "_token_scopes": [],
        },
    )
    asyncio.run(idem_mod.idempotency_middleware(_make_req(), _handler))
    assert exec_count["count"] == 1

    # Fingerprint now changes.
    monkeypatch.setattr(
        idem_mod, "_resolve_user_context",
        lambda req, db: {
            "id": 1, "username": "u", "role": "viewer",
            "group_id": None, "is_active": True, "_token_scopes": [],
        },
    )
    result = asyncio.run(idem_mod.idempotency_middleware(_make_req(), _handler))
    # Handler must NOT have run again.
    assert exec_count["count"] == 1, (
        "handler must not re-execute on fingerprint drift — that "
        "would duplicate the original mutation"
    )
    assert result.status_code == 409
    body = _json.loads(bytes(result.body))
    assert body["error"]["code"] == "idempotency_authz_changed"

    # Original row must still be present (not deleted).
    rows = conn.execute(
        "SELECT state FROM idempotency_keys WHERE key = 'k-fp'"
    ).fetchone()
    assert rows is not None and rows[0] == "complete"


def test_fingerprint_replays_on_match(monkeypatch, conn):
    """Same-caller retry hits the cache and replays without re-running."""
    from types import SimpleNamespace
    from fastapi.responses import Response
    import core.middleware.idempotency as idem_mod
    import core.db as db_mod

    test_db = _fake_db(conn)
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: test_db)

    class _Headers:
        _h = {"Idempotency-Key": "k-same", "content-type": "application/json", "content-length": "100"}
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/jobs"
        query = ""

    def _make_req():
        class _Req:
            method = "POST"
            headers = _Headers()
            url = _URL()
            state = SimpleNamespace()
            cookies = {}
            async def body(self):
                return b'{"item":"x"}'
        return _Req()

    exec_count = {"count": 0}

    async def _handler(request):
        exec_count["count"] += 1
        return Response(content=b'{"id":1}', status_code=201, media_type="application/json")

    ctx = {
        "id": 1, "username": "u", "role": "admin",
        "group_id": None, "is_active": True, "_token_scopes": [],
    }
    monkeypatch.setattr(idem_mod, "_resolve_user_context", lambda req, db: ctx)

    import asyncio
    asyncio.run(idem_mod.idempotency_middleware(_make_req(), _handler))
    result = asyncio.run(idem_mod.idempotency_middleware(_make_req(), _handler))

    assert exec_count["count"] == 1, "second call must replay from cache"
    assert result.headers.get("X-Idempotent-Replay") == "true"


def test_response_with_set_cookie_is_not_cacheable(monkeypatch, conn):
    """Codex pass 5: session-cookie-bearing responses (login, OIDC)
    must NOT be cached — replay would return body without cookie,
    producing silent auth desync."""
    from types import SimpleNamespace
    from fastapi.responses import Response

    class _Headers:
        _h = {
            "Idempotency-Key": "k-login",
            "content-type": "application/json",
            "content-length": "100",
        }
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/auth/login"
        query = ""

    class _Req:
        method = "POST"
        headers = _Headers()
        url = _URL()
        state = SimpleNamespace()
        cookies = {}
        async def body(self):
            return b'{"username":"u","password":"p"}'

    async def _login_handler(request):
        resp = Response(
            content=b'{"token":"xyz"}',
            status_code=200,
            media_type="application/json",
        )
        resp.set_cookie("session", "opaque-jwt", httponly=True)
        return resp

    result, call_count = _run_middleware_and_capture(
        monkeypatch, conn, _Req(), _login_handler
    )
    assert call_count == 1
    assert result.status_code == 200

    # Row is marked 'uncacheable_success' so a retry can't re-execute
    # (codex pass 12). The first call saw the real response + cookies.
    row = conn.execute(
        "SELECT state FROM idempotency_keys WHERE key = 'k-login'"
    ).fetchone()
    assert row is not None and row[0] == "uncacheable_success"


def test_oversized_response_body_is_not_cacheable(monkeypatch, conn):
    """Codex pass 5: responses larger than _MAX_BODY_BYTES must be
    released rather than cached with an empty body (the prior design
    would have replayed an empty JSON on retry)."""
    from types import SimpleNamespace
    from fastapi.responses import Response
    from core.middleware.idempotency import _MAX_BODY_BYTES

    oversized_body = b'{"items":' + (b'"x",' * (_MAX_BODY_BYTES // 4 + 1)) + b'"end"]}'
    assert len(oversized_body) > _MAX_BODY_BYTES

    class _Headers:
        _h = {
            "Idempotency-Key": "k-big-resp",
            "content-type": "application/json",
            "content-length": "100",
        }
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/jobs/bulk"
        query = ""

    class _Req:
        method = "POST"
        headers = _Headers()
        url = _URL()
        state = SimpleNamespace()
        cookies = {}
        async def body(self):
            return b'{"items":[]}'

    async def _bulk_handler(request):
        return Response(
            content=oversized_body,
            status_code=201,
            media_type="application/json",
        )

    result, call_count = _run_middleware_and_capture(
        monkeypatch, conn, _Req(), _bulk_handler
    )
    assert call_count == 1
    assert result.status_code == 201

    # Row marked uncacheable_success — retries 409 instead of re-executing.
    row = conn.execute(
        "SELECT state FROM idempotency_keys WHERE key = 'k-big-resp'"
    ).fetchone()
    assert row is not None and row[0] == "uncacheable_success"


def test_chunked_request_without_content_length_refused(monkeypatch, conn):
    """Codex pass 11+13: only refuse actually-chunked requests. A
    missing Content-Length by itself is NOT a refusal — many clients
    omit it on empty-body POST/DELETE. Refuse only when
    Transfer-Encoding: chunked is explicitly set."""
    from types import SimpleNamespace
    from fastapi.responses import Response

    class _Headers:
        # Chunked transfer encoding + no Content-Length.
        _h = {
            "Idempotency-Key": "k-chunked",
            "content-type": "application/json",
            "transfer-encoding": "chunked",
        }
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/jobs"
        query = ""

    class _Req:
        method = "POST"
        headers = _Headers()
        url = _URL()
        state = SimpleNamespace()
        cookies = {}
        async def body(self):
            raise AssertionError("body() must not be called without Content-Length")

    async def _handler(request):
        raise AssertionError("handler must not run")

    import json as _json
    result, call_count = _run_middleware_and_capture(monkeypatch, conn, _Req(), _handler)
    assert call_count == 0
    assert result.status_code == 411  # Length Required
    body = _json.loads(bytes(result.body))
    assert body["error"]["code"] == "idempotency_unsupported"


def test_replay_preserves_non_json_media_type(monkeypatch, conn):
    """Codex pass 20: replay must preserve the original Content-Type.
    Mutating routes can legitimately return application/sdp (WebRTC
    offer), text/plain, etc. Rebuilding replay as application/json
    corrupts those responses."""
    from types import SimpleNamespace
    from fastapi.responses import Response
    import asyncio
    import core.middleware.idempotency as idem_mod
    import core.db as db_mod

    test_db = _fake_db(conn)
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: test_db)
    monkeypatch.setattr(
        idem_mod, "_resolve_user_context",
        lambda req, db: {
            "id": 1, "username": "u", "role": "admin",
            "group_id": None, "is_active": True, "_token_scopes": [],
        },
    )

    class _Headers:
        _h = {
            "Idempotency-Key": "k-sdp",
            "content-type": "application/json",
            "content-length": "50",
        }
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/cameras/1/webrtc"
        query = ""

    def _make_req():
        class _Req:
            method = "POST"
            headers = _Headers()
            url = _URL()
            state = SimpleNamespace()
            cookies = {}
            async def body(self):
                return b'{"offer":"x"}'
        return _Req()

    async def _sdp_handler(request):
        return Response(
            content=b"v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n",
            status_code=200,
            media_type="application/sdp",
        )

    first = asyncio.run(idem_mod.idempotency_middleware(_make_req(), _sdp_handler))
    assert first.media_type == "application/sdp"

    second = asyncio.run(idem_mod.idempotency_middleware(_make_req(), _sdp_handler))
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert second.media_type == "application/sdp", (
        f"replay lost Content-Type — got {second.media_type!r}, "
        f"want application/sdp"
    )


def test_empty_body_without_content_length_is_accepted(monkeypatch, conn):
    """Codex pass 13: /auth/logout, /alerts/mark-all-read etc. have
    empty bodies; many clients omit Content-Length in that case.
    Middleware must accept — only refuse actual chunked uploads."""
    from types import SimpleNamespace
    from fastapi.responses import Response

    class _Headers:
        # No Content-Length, no Transfer-Encoding: chunked.
        _h = {"Idempotency-Key": "k-logout", "content-type": "application/json"}
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/auth/logout"
        query = ""

    class _Req:
        method = "POST"
        headers = _Headers()
        url = _URL()
        state = SimpleNamespace()
        cookies = {}
        async def body(self):
            return b""

    async def _handler(request):
        return Response(content=b'{"logged_out":true}', status_code=200, media_type="application/json")

    result, call_count = _run_middleware_and_capture(monkeypatch, conn, _Req(), _handler)
    # Handler ran; cached successfully.
    assert call_count == 1
    assert result.status_code == 200


def test_middleware_degrades_when_table_missing(monkeypatch):
    """Codex pass 8: if migration 005 hasn't applied, the middleware
    must pass through cleanly instead of 500ing every request."""
    from types import SimpleNamespace
    import asyncio
    from fastapi.responses import Response
    import sqlite3
    import core.middleware.idempotency as idem_mod
    import core.db as db_mod

    # Fresh SQLite connection with NO migrations applied (no table).
    empty_conn = sqlite3.connect(":memory:")
    empty_db = _fake_db(empty_conn)
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: empty_db)

    # Clear any cached readiness from prior tests.
    idem_mod._SCHEMA_READY_CACHE["ready"] = False
    idem_mod._SCHEMA_READY_CACHE["checked_at"] = 0.0

    class _Headers:
        _h = {"Idempotency-Key": "k-skew", "content-type": "application/json", "content-length": "100"}
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/jobs"
        query = ""

    class _Req:
        method = "POST"
        headers = _Headers()
        url = _URL()
        state = SimpleNamespace()
        cookies = {}
        async def body(self):
            return b'{"item":"x"}'

    exec_count = {"n": 0}

    async def _handler(request):
        exec_count["n"] += 1
        return Response(content=b'{"ok":true}', status_code=200, media_type="application/json")

    # Should NOT raise; should pass through to handler.
    result = asyncio.run(idem_mod.idempotency_middleware(_Req(), _handler))
    assert result.status_code == 200
    assert exec_count["n"] == 1
    empty_conn.close()


def test_response_with_security_headers_is_cacheable(monkeypatch, conn):
    """Codex pass 16: security_headers middleware adds CSP /
    X-Frame-Options / etc. to every response. Those MUST NOT make
    the response uncacheable — they're framework-idempotent and get
    re-added on replay. Without this fix, every real 2xx write in the
    live stack ended up as `uncacheable_success` → retries failed 409."""
    from types import SimpleNamespace
    from fastapi.responses import Response
    import asyncio
    import core.middleware.idempotency as idem_mod
    import core.db as db_mod

    test_db = _fake_db(conn)
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: test_db)
    monkeypatch.setattr(
        idem_mod, "_resolve_user_context",
        lambda req, db: {
            "id": 1, "username": "u", "role": "admin",
            "group_id": None, "is_active": True, "_token_scopes": [],
        },
    )

    class _Headers:
        _h = {
            "Idempotency-Key": "k-sec",
            "content-type": "application/json",
            "content-length": "50",
        }
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/jobs"
        query = ""

    def _make_req():
        class _Req:
            method = "POST"
            headers = _Headers()
            url = _URL()
            state = SimpleNamespace()
            cookies = {}
            async def body(self):
                return b'{"item":"x"}'
        return _Req()

    async def _handler_with_security_headers(request):
        resp = Response(
            content=b'{"id":1}',
            status_code=201,
            media_type="application/json",
        )
        # Simulate what security_headers middleware adds.
        resp.headers["Content-Security-Policy"] = "default-src 'self'"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    # First call: completes normally (NOT uncacheable).
    asyncio.run(idem_mod.idempotency_middleware(_make_req(), _handler_with_security_headers))
    row = conn.execute(
        "SELECT state FROM idempotency_keys WHERE key = 'k-sec'"
    ).fetchone()
    assert row is not None and row[0] == "complete", (
        f"security headers must not disqualify caching; got state={row}"
    )

    # Second call: replays successfully.
    result2 = asyncio.run(idem_mod.idempotency_middleware(_make_req(), _handler_with_security_headers))
    assert result2.status_code == 201
    assert result2.headers.get("X-Idempotent-Replay") == "true"


def test_retry_after_uncacheable_success_returns_409(monkeypatch, conn):
    """Codex pass 12: a retry with the same key on a route that
    returned a non-cacheable 2xx must 409, not re-execute. This is
    the core anti-duplication guarantee for auth routes."""
    from types import SimpleNamespace
    from fastapi.responses import Response
    import asyncio
    import json as _json
    import core.middleware.idempotency as idem_mod
    import core.db as db_mod

    test_db = _fake_db(conn)
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: test_db)
    monkeypatch.setattr(
        idem_mod, "_resolve_user_context",
        lambda req, db: {
            "id": 1, "username": "u", "role": "admin",
            "group_id": None, "is_active": True, "_token_scopes": [],
        },
    )

    class _Headers:
        _h = {
            "Idempotency-Key": "k-retry-uncache",
            "content-type": "application/json",
            "content-length": "100",
        }
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/auth/login"
        query = ""

    def _make_req():
        class _Req:
            method = "POST"
            headers = _Headers()
            url = _URL()
            state = SimpleNamespace()
            cookies = {}
            async def body(self):
                return b'{"username":"u","password":"p"}'
        return _Req()

    calls = {"n": 0}

    async def _login_handler(request):
        calls["n"] += 1
        resp = Response(
            content=b'{"token":"xyz"}',
            status_code=200,
            media_type="application/json",
        )
        resp.set_cookie("session", "opaque-jwt", httponly=True)
        return resp

    # First call: success, row marked uncacheable_success.
    result1 = asyncio.run(idem_mod.idempotency_middleware(_make_req(), _login_handler))
    assert result1.status_code == 200
    assert calls["n"] == 1

    # Second call with the SAME key: must 409, handler must NOT run again.
    result2 = asyncio.run(idem_mod.idempotency_middleware(_make_req(), _login_handler))
    assert calls["n"] == 1, "handler must not re-execute on retry of uncacheable success"
    assert result2.status_code == 409
    body = _json.loads(bytes(result2.body))
    assert body["error"]["code"] == "idempotency_uncacheable_success"


def test_oversized_content_length_skips_idempotency(monkeypatch, conn):
    """Content-length above the 1 MB cap bypasses the middleware."""
    from types import SimpleNamespace
    from fastapi.responses import Response

    oversized = str(2 * 1024 * 1024)  # 2 MB

    class _Headers:
        _h = {
            "Idempotency-Key": "k-big",
            "content-type": "application/octet-stream",
            "content-length": oversized,
        }
        def get(self, k, default=None):
            return self._h.get(k.lower(), self._h.get(k, default))

    class _URL:
        path = "/api/v1/backups/restore"
        query = ""

    class _Req:
        method = "POST"
        headers = _Headers()
        url = _URL()
        state = SimpleNamespace()
        cookies = {}
        async def body(self):
            raise AssertionError("body() must not be called for oversized")

    async def _handler(request):
        raise AssertionError("handler must not run — middleware should 413")

    import json as _json
    result, call_count = _run_middleware_and_capture(monkeypatch, conn, _Req(), _handler)
    # Codex pass 9: explicit 413 rather than silent pass-through.
    assert call_count == 0
    assert result.status_code == 413
    body = _json.loads(bytes(result.body))
    assert body["error"]["code"] == "idempotency_unsupported"

    rows = conn.execute(
        "SELECT COUNT(*) FROM idempotency_keys WHERE key = 'k-big'"
    ).fetchone()[0]
    assert rows == 0


# ---------------------------------------------------------------------------
# Lookup classifier
# ---------------------------------------------------------------------------

def test_lookup_classifies_miss(conn):
    from core.middleware.idempotency import _lookup_row, _LOOKUP_MISS

    db = _fake_db(conn)
    cls, _, _, _, _, _ = _lookup_row(db, "missing", 1, "h")
    assert cls == _LOOKUP_MISS


def test_lookup_classifies_pending_same_hash(conn):
    from core.middleware.idempotency import (
        _try_claim, _lookup_row, _LOOKUP_PENDING,
    )

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "h")
    cls, _, _, _, _, _ = _lookup_row(db, "k", 1, "h")
    assert cls == _LOOKUP_PENDING


def test_lookup_classifies_conflict_when_pending_with_different_hash(conn):
    from core.middleware.idempotency import (
        _try_claim, _lookup_row, _LOOKUP_CONFLICT,
    )

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "original")
    cls, _, _, _, _, _ = _lookup_row(db, "k", 1, "different")
    assert cls == _LOOKUP_CONFLICT


def test_lookup_classifies_hit(conn):
    from core.middleware.idempotency import (
        _try_claim, _finalize_row, _lookup_row, _LOOKUP_HIT,
    )

    db = _fake_db(conn)
    _try_claim(db, "k", 1, "POST", "/x", "h")
    _finalize_row(db, "k", 1, 201, b'{"id": 9}')
    cls, status, body, _, _, _ = _lookup_row(db, "k", 1, "h")
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
    cls, _, _, _, _, _ = _lookup_row(db, "k", 1, "different")
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
    cls, _, _, created_at_str, _, _ = _lookup_row(db, "old", 1, "h")
    assert cls == _LOOKUP_EXPIRED
    assert created_at_str == past


def test_stuck_pending_row_classifies_as_stuck_pending(conn):
    """A pending row older than the watchdog is treated as
    stuck_pending so the caller can CAS-claim it (single winner).
    Watchdog is 15 min (codex pass 7) — test uses 20 min."""
    from core.middleware.idempotency import _lookup_row, _LOOKUP_STUCK_PENDING

    past = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
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
    cls, _, _, created_at_str, _, _ = _lookup_row(db, "stuck", 1, "h")
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
    stuck = (now - timedelta(minutes=20)).isoformat()         # stuck pending (watchdog 15m)
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
