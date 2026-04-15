"""Idempotency-Key middleware for agent-safe retries.

The Idempotency-Key pattern (RFC-style, used by Stripe/GitHub/etc.):
a client sends `Idempotency-Key: <uuid>` on a mutating request
(POST/PUT/PATCH/DELETE). The server caches the response for N hours.
If the same key comes back with the same request body, the server
replays the cached response instead of executing the operation twice.

This is load-bearing for agent-driven operation. A weak local LLM
that "forgets" it already fired a `queue_job` tool and retries would
otherwise create duplicate jobs. With Idempotency-Key, the retry is
a no-op.

Scope and semantics:
- Enforced on POST/PUT/PATCH/DELETE only. GETs are idempotent by
  definition; no need to cache.
- Keyed on (key, user_id). Two distinct users with the same UUID
  (vanishingly unlikely but non-zero) do not cross-replay.
- request_hash = sha256(method + path + sorted-body). If the same key
  arrives with a *different* hash, the middleware returns 409
  Conflict — that's a client bug, not a silent different-result
  replay.
- Cache TTL is 24h. Rows are pruned by an hourly scheduler job;
  stale rows are also rejected at read time so a delayed pruner
  cannot leak stale responses.
- If the request is anonymous (no auth credentials resolvable to a
  user_id), the middleware passes through without caching. The
  downstream auth check will reject the request; idempotency-for-
  anonymous would either dedupe across different users (unsafe) or
  not dedupe at all (pointless).

Not enforced:
- Streaming responses. The middleware reads the response body into
  memory; for large streams we skip caching (see `_MAX_BODY_BYTES`).
  In practice all ODIN mutating responses are small JSON envelopes.
- Content negotiation. Cache is keyed on the request hash; serving
  a different content-type on replay is not a concern because the
  cached response records its own status + body verbatim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

# FastAPI types are only needed inside the middleware coroutine at
# runtime. Importing them at module level would force FastAPI to be a
# test-suite dependency for every helper that only touches the DB or
# hash algorithm — an unnecessary coupling. The middleware function
# imports them lazily.

log = logging.getLogger("odin.middleware.idempotency")

# Cache responses up to 256 KB. Larger bodies skip caching — this is a
# safety net; ODIN write-endpoint responses are all small envelopes.
_MAX_BODY_BYTES = 256 * 1024

# 24-hour cache TTL. Hourly prune runs on top of this; the read-side
# check enforces the TTL even if the pruner is late or skipped.
_TTL_HOURS = 24

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _resolve_user_id(request: Any, db: Session) -> Optional[int]:
    """Lightweight user-id lookup that mirrors `get_current_user`.

    Only needs enough of the logic to pin a user_id for scoping the
    idempotency cache. Full auth (expiry check, MFA, etc.) happens in
    the route's own `Depends(get_current_user)` — we just need a stable
    identity handle.

    Returns None if the request is anonymous or the credential doesn't
    resolve. In that case the middleware passes through without caching.
    """
    # JWT bearer
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            from core.auth import decode_access_token
            token = auth[7:]
            payload = decode_access_token(token)
            username = payload.get("sub")
            if username:
                row = db.execute(
                    text("SELECT id FROM users WHERE username = :u"),
                    {"u": username},
                ).fetchone()
                if row:
                    return int(row[0])
        except Exception:
            return None

    # X-API-Key — per-user scoped token
    api_key = request.headers.get("X-API-Key", "")
    if api_key.startswith("odin_"):
        try:
            from core.auth import verify_password
            prefix = api_key[:10]
            candidates = db.execute(
                text("SELECT id, user_id, token_hash FROM api_tokens WHERE token_prefix = :p"),
                {"p": prefix},
            ).fetchall()
            for row in candidates:
                if verify_password(api_key, row.token_hash):
                    return int(row.user_id)
        except Exception:
            return None

    # X-API-Key — global legacy key maps to "first admin"
    if api_key and api_key != "undefined":
        configured = os.getenv("API_KEY", "")
        if configured and hmac.compare_digest(api_key, configured):
            row = db.execute(
                text(
                    "SELECT id FROM users WHERE role = 'admin' "
                    "AND is_active = 1 ORDER BY id LIMIT 1"
                )
            ).fetchone()
            if row:
                return int(row[0])

    return None


def _compute_request_hash(method: str, path: str, body: bytes) -> str:
    """Deterministic hash over the request identity + body.

    JSON bodies are canonicalized (sorted keys) before hashing so that
    `{"a":1,"b":2}` and `{"b":2,"a":1}` are considered equivalent and
    don't false-409 the client.
    """
    payload_bytes = body or b""
    if payload_bytes:
        try:
            parsed = json.loads(payload_bytes.decode("utf-8"))
            payload_bytes = json.dumps(parsed, sort_keys=True).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Non-JSON body (e.g. multipart upload) — hash verbatim.
            pass

    h = hashlib.sha256()
    h.update(method.encode("ascii"))
    h.update(b"\x00")
    h.update(path.encode("utf-8"))
    h.update(b"\x00")
    h.update(payload_bytes)
    return h.hexdigest()


def _lookup_cached(
    db: Session,
    key: str,
    user_id: int,
    request_hash: str,
) -> Optional[tuple[int, str, bool]]:
    """Look up a cached response for (key, user_id).

    Returns (status_code, body_text, is_conflict) or None if no match.
    `is_conflict=True` means the key exists but the body hash differs
    from the current request — the caller returns 409.
    """
    row = db.execute(
        text(
            "SELECT request_hash, response_status, response_body, created_at "
            "FROM idempotency_keys "
            "WHERE key = :k AND user_id = :u"
        ),
        {"k": key, "u": user_id},
    ).fetchone()
    if not row:
        return None

    created_at = row[3]
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            # Corrupt timestamp — treat as expired.
            return None

    # Normalize to aware UTC for the TTL compare.
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) - created_at > timedelta(hours=_TTL_HOURS):
        # Expired row — do not replay. The pruner cleans up eventually.
        return None

    if row[0] != request_hash:
        return 0, "", True

    return int(row[1]), row[2], False


def _store_cached(
    db: Session,
    key: str,
    user_id: int,
    method: str,
    path: str,
    request_hash: str,
    response_status: int,
    response_body: bytes,
) -> None:
    """Persist the response in the idempotency cache."""
    if len(response_body) > _MAX_BODY_BYTES:
        log.debug("idempotency cache skipped — body %d bytes exceeds max", len(response_body))
        return

    try:
        body_text = response_body.decode("utf-8")
    except UnicodeDecodeError:
        log.debug("idempotency cache skipped — response body not UTF-8")
        return

    try:
        db.execute(
            text(
                "INSERT INTO idempotency_keys "
                "(key, user_id, method, path, request_hash, response_status, response_body) "
                "VALUES (:k, :u, :m, :p, :h, :s, :b)"
            ),
            {
                "k": key,
                "u": user_id,
                "m": method,
                "p": path,
                "h": request_hash,
                "s": response_status,
                "b": body_text,
            },
        )
        db.commit()
    except Exception as exc:
        # Race: another request with the same key+user_id committed
        # first. That's fine — we can read their row on replay.
        db.rollback()
        log.debug("idempotency cache insert failed (likely race): %s", exc)


async def idempotency_middleware(request: Any, call_next: Callable):
    """FastAPI HTTP middleware: cache-by-Idempotency-Key for mutators.

    Wired into the app factory via `app.middleware("http")`.
    """
    from fastapi.responses import Response  # noqa: WPS433 — see module docstring

    # Fast-path: non-mutating method. No work.
    if request.method not in _MUTATING_METHODS:
        return await call_next(request)

    key = request.headers.get("Idempotency-Key")
    if not key:
        return await call_next(request)

    # Body must be read before dispatch; FastAPI already buffers it for
    # most routes, but we ensure it's available for hashing and then
    # put it back so downstream handlers see it.
    body = await request.body()

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = _receive  # type: ignore[assignment]

    # DB handle. Use a short-lived session so we don't tangle with the
    # route's own session lifecycle.
    from core.db import SessionLocal
    db = SessionLocal()
    try:
        user_id = _resolve_user_id(request, db)
        if user_id is None:
            # Anonymous / unresolvable — pass through; auth will reject
            # downstream and we don't want to cache under a null key.
            return await call_next(request)

        request_hash = _compute_request_hash(
            request.method, request.url.path, body
        )
        cached = _lookup_cached(db, key, user_id, request_hash)
        if cached is not None:
            status_code, body_text, is_conflict = cached
            if is_conflict:
                return Response(
                    content=json.dumps({
                        "error": {
                            "code": "idempotency_conflict",
                            "detail": (
                                "Idempotency-Key already used with a different "
                                "request body. Use a fresh key for a new request."
                            ),
                            "retriable": False,
                        }
                    }),
                    status_code=409,
                    media_type="application/json",
                )
            # Cached hit — replay.
            return Response(
                content=body_text,
                status_code=status_code,
                media_type="application/json",
                headers={"X-Idempotent-Replay": "true"},
            )

        # Cache miss — execute the route and capture the response body.
        response = await call_next(request)

        # Only cache successful writes (2xx). Errors should be
        # retried freshly because the cause may be transient.
        if 200 <= response.status_code < 300:
            # Response bodies from FastAPI are iterators; drain to bytes.
            body_chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                body_chunks.append(chunk)
            captured = b"".join(body_chunks)

            _store_cached(
                db=db,
                key=key,
                user_id=user_id,
                method=request.method,
                path=request.url.path,
                request_hash=request_hash,
                response_status=response.status_code,
                response_body=captured,
            )

            # Re-emit the response to the client with the captured body.
            return Response(
                content=captured,
                status_code=response.status_code,
                media_type=response.media_type,
                headers=dict(response.headers),
            )

        return response
    finally:
        db.close()


def prune_expired_idempotency_keys(db: Session) -> int:
    """Delete rows older than the TTL.

    Called hourly by the scheduler. Returns the number of rows deleted
    so the scheduler can log activity.

    Cutoff is passed as an ISO-8601 string rather than a datetime
    object because SQLite's default datetime adapter (deprecated as of
    Python 3.12) was stripping the UTC offset and producing silent
    zero-match comparisons against naive ISO strings written by
    `_store_cached`. String-on-string comparison is stable on both
    SQLite and Postgres and matches the column's TEXT storage.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=_TTL_HOURS)
    cutoff_iso = cutoff_dt.isoformat()
    result = db.execute(
        text("DELETE FROM idempotency_keys WHERE created_at < :cutoff"),
        {"cutoff": cutoff_iso},
    )
    db.commit()
    try:
        return int(result.rowcount or 0)
    except Exception:
        return 0
