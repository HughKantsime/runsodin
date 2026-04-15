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

Design (post-codex-review, 2026-04-14):
- **Claim-before-execute.** The middleware INSERTs a pending row
  BEFORE running the handler. If two concurrent same-key requests
  race, only one wins the INSERT; the loser sees `state='pending'`
  and returns 409 `idempotency_in_progress` instead of executing a
  second mutation. The winner runs the handler and UPDATEs the row
  to `state='complete'` with the real response.
- **Auth must pass before replay.** Replay returns a cached 2xx
  body only after the request has passed the app's auth chain in
  the live direction (the middleware is registered INSIDE the auth
  layer — see `_register_http_middleware` in core/app.py). A
  request with an expired/revoked token hits 401 from auth before
  the middleware ever consults the cache.
- **Per-user PK scope.** Keyed on (key, user_id). Two distinct users
  with the same UUID (vanishingly unlikely but non-zero) do not
  cross-replay.
- **request_hash pinning.** `request_hash = sha256(method + path +
  canonicalized-body)`. If the same key arrives with a different
  hash, the middleware returns 409 `idempotency_conflict` — that's
  a client bug, not a silent different-result replay.
- **TTL.** 24h for completed rows. Pending rows expire after 90s so
  a crashed handler can't wedge the key forever. Both are enforced
  at read time AND by the hourly pruner.

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

# Completed-row TTL. Replays within this window; pruner cleans up.
_TTL_HOURS = 24

# Pending-row watchdog: if a handler crashes without writing the
# response, the key would be wedged until the pruner runs. 90s lets a
# healthy handler finish (even slow ones) while bounding the stuck
# window. A stuck pending row is treated as a cache miss on the next
# read.
_PENDING_WATCHDOG_SECONDS = 90

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_STATE_PENDING = "pending"
_STATE_COMPLETE = "complete"


def _resolve_user_id(request: Any, db: Session) -> Optional[int]:
    """Lightweight user-id lookup — must check expiry + active status.

    Codex pass 1 (2026-04-14) flagged that replay can happen with an
    expired/revoked token if the middleware only does a prefix+hash
    match and skips the checks `get_current_user` applies. The primary
    defense is middleware registration order (auth runs before this),
    but as a belt-and-suspenders check we also enforce:
      - token not past `expires_at`
      - user `is_active=1`

    Returns None if the request is anonymous, the credential doesn't
    resolve, or any check fails. The middleware then passes through
    without touching the cache; downstream auth will reject the
    request.
    """
    # JWT bearer
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            from core.auth import decode_access_token
            token = auth[7:]
            payload = decode_access_token(token)
            username = payload.get("sub")
            if not username:
                return None
            row = db.execute(
                text("SELECT id, is_active FROM users WHERE username = :u"),
                {"u": username},
            ).fetchone()
            if not row:
                return None
            if not row.is_active:
                return None
            return int(row.id)
        except Exception:
            return None

    # X-API-Key — per-user scoped token
    api_key = request.headers.get("X-API-Key", "")
    if api_key.startswith("odin_"):
        try:
            from core.auth import verify_password
            prefix = api_key[:10]
            candidates = db.execute(
                text(
                    "SELECT id, user_id, token_hash, expires_at "
                    "FROM api_tokens WHERE token_prefix = :p"
                ),
                {"p": prefix},
            ).fetchall()
            now = datetime.now(timezone.utc)
            for row in candidates:
                if not verify_password(api_key, row.token_hash):
                    continue
                if row.expires_at:
                    try:
                        from dateutil.parser import parse as parse_dt
                        exp = (
                            parse_dt(row.expires_at)
                            if isinstance(row.expires_at, str)
                            else row.expires_at
                        )
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=timezone.utc)
                        if exp < now:
                            return None
                    except Exception:
                        return None
                # Ensure the owning user is still active.
                user_row = db.execute(
                    text("SELECT is_active FROM users WHERE id = :id"),
                    {"id": row.user_id},
                ).fetchone()
                if not user_row or not user_row.is_active:
                    return None
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


def _parse_created_at(raw: Any) -> Optional[datetime]:
    """Best-effort parse of the `created_at` column across drivers."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Sentinels for the lookup classifier below.
_LOOKUP_MISS = "miss"
_LOOKUP_PENDING = "pending"
_LOOKUP_CONFLICT = "conflict"
_LOOKUP_HIT = "hit"
_LOOKUP_EXPIRED = "expired"


def _lookup_row(
    db: Session, key: str, user_id: int, request_hash: str
) -> tuple[str, Optional[int], Optional[str]]:
    """Look up the cache row and classify its state.

    Returns (classification, status, body). `status`/`body` are only
    populated when classification == hit.

    - `miss`: no row, or the previous row is stale and should be
      treated as absent (caller re-claims).
    - `pending`: another in-flight request holds the key; caller
      returns 409 in_progress.
    - `conflict`: row exists with a different request hash; caller
      returns 409 conflict.
    - `hit`: cached response available for replay.
    - `expired`: completed row is past TTL; caller treats as miss and
      overwrites (claim-again path).
    """
    row = db.execute(
        text(
            "SELECT request_hash, state, response_status, response_body, "
            "created_at, updated_at "
            "FROM idempotency_keys "
            "WHERE key = :k AND user_id = :u"
        ),
        {"k": key, "u": user_id},
    ).fetchone()
    if not row:
        return _LOOKUP_MISS, None, None

    state = row.state
    now = datetime.now(timezone.utc)

    if state == _STATE_PENDING:
        created = _parse_created_at(row.created_at)
        if created is None:
            return _LOOKUP_MISS, None, None
        # Stuck pending row — treat as absent so the caller can re-claim.
        if now - created > timedelta(seconds=_PENDING_WATCHDOG_SECONDS):
            log.debug(
                "idempotency: pending row older than %ss — treating as miss",
                _PENDING_WATCHDOG_SECONDS,
            )
            return _LOOKUP_MISS, None, None
        # Still in-flight. Caller returns 409 in_progress.
        if row.request_hash != request_hash:
            return _LOOKUP_CONFLICT, None, None
        return _LOOKUP_PENDING, None, None

    # state == complete
    if row.request_hash != request_hash:
        return _LOOKUP_CONFLICT, None, None

    created = _parse_created_at(row.created_at)
    if created is None:
        return _LOOKUP_MISS, None, None
    if now - created > timedelta(hours=_TTL_HOURS):
        return _LOOKUP_EXPIRED, None, None

    return _LOOKUP_HIT, int(row.response_status), row.response_body


def _try_claim(
    db: Session,
    key: str,
    user_id: int,
    method: str,
    path: str,
    request_hash: str,
) -> bool:
    """Atomically claim the key by inserting a pending row.

    Returns True if we own the key (handler should run). False if the
    row already existed (caller re-reads to see why — pending, hit,
    conflict, or expired).

    The insert is guarded by the PK; concurrent attempts resolve
    deterministically: exactly one INSERT succeeds, the rest raise
    IntegrityError. Stuck-pending rows are handled separately by the
    caller (treats them as miss and UPDATEs state back to pending
    with the new metadata).
    """
    try:
        db.execute(
            text(
                "INSERT INTO idempotency_keys "
                "(key, user_id, method, path, request_hash, state, "
                " response_status, response_body) "
                "VALUES (:k, :u, :m, :p, :h, 'pending', 0, '')"
            ),
            {
                "k": key,
                "u": user_id,
                "m": method,
                "p": path,
                "h": request_hash,
            },
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False


def _reclaim_expired(
    db: Session,
    key: str,
    user_id: int,
    method: str,
    path: str,
    request_hash: str,
) -> bool:
    """Overwrite a stuck-pending or TTL-expired row back to pending.

    Caller has already classified the row as miss/expired and wants
    to claim it. Update-in-place preserves the PK.
    """
    now = datetime.now(timezone.utc)
    try:
        db.execute(
            text(
                "UPDATE idempotency_keys "
                "SET method = :m, path = :p, request_hash = :h, "
                "    state = 'pending', response_status = 0, "
                "    response_body = '', "
                "    created_at = :now, updated_at = :now "
                "WHERE key = :k AND user_id = :u"
            ),
            {
                "k": key, "u": user_id, "m": method, "p": path, "h": request_hash,
                "now": now.isoformat(),
            },
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False


def _finalize_row(
    db: Session,
    key: str,
    user_id: int,
    response_status: int,
    response_body: bytes,
) -> None:
    """UPDATE the pending row to complete with the real response."""
    if len(response_body) > _MAX_BODY_BYTES:
        # Too big to cache — mark the row complete with an empty body so
        # replay returns a sensible empty response rather than
        # reserving the key forever. In practice all agent-surface
        # responses are tiny; this path is a safety net.
        body_text = ""
    else:
        try:
            body_text = response_body.decode("utf-8")
        except UnicodeDecodeError:
            body_text = ""

    try:
        db.execute(
            text(
                "UPDATE idempotency_keys "
                "SET state = 'complete', "
                "    response_status = :s, "
                "    response_body = :b, "
                "    updated_at = :now "
                "WHERE key = :k AND user_id = :u"
            ),
            {
                "k": key, "u": user_id,
                "s": response_status, "b": body_text,
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        log.debug("idempotency finalize failed: %s", exc)


def _release_row(db: Session, key: str, user_id: int) -> None:
    """Delete the pending row after the handler returned non-2xx.

    We cache only successful writes (2xx) — errors should be retried
    freshly because the cause may be transient. Releasing the row
    frees the key for a fresh attempt.
    """
    try:
        db.execute(
            text(
                "DELETE FROM idempotency_keys "
                "WHERE key = :k AND user_id = :u AND state = 'pending'"
            ),
            {"k": key, "u": user_id},
        )
        db.commit()
    except Exception:
        db.rollback()


def _conflict_response(code: str, detail: str):
    """Build the 409 envelope for in-progress / conflict cases."""
    from fastapi.responses import Response  # noqa: WPS433
    return Response(
        content=json.dumps({
            "error": {
                "code": code,
                "detail": detail,
                "retriable": code == "idempotency_in_progress",
            }
        }),
        status_code=409,
        media_type="application/json",
    )


async def idempotency_middleware(request: Any, call_next: Callable):
    """FastAPI HTTP middleware: cache-by-Idempotency-Key for mutators.

    Wired into the app factory via `app.middleware("http")`. Must be
    registered INSIDE the auth middleware so cached-replay requests
    still pass auth validation (token expiry, active user, scope).
    """
    from fastapi.responses import Response  # noqa: WPS433

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

    from core.db import SessionLocal
    db = SessionLocal()
    try:
        user_id = _resolve_user_id(request, db)
        if user_id is None:
            # Anonymous / unresolvable / expired — pass through; auth
            # will reject downstream and we don't want to cache under
            # a null key.
            return await call_next(request)

        request_hash = _compute_request_hash(
            request.method, request.url.path, body
        )

        classification, status, body_text = _lookup_row(
            db, key, user_id, request_hash
        )

        if classification == _LOOKUP_CONFLICT:
            return _conflict_response(
                "idempotency_conflict",
                "Idempotency-Key already used with a different request body. "
                "Use a fresh key for a new request.",
            )

        if classification == _LOOKUP_PENDING:
            return _conflict_response(
                "idempotency_in_progress",
                "Another request with this Idempotency-Key is in flight. "
                "Retry in a moment — this is retriable.",
            )

        if classification == _LOOKUP_HIT:
            return Response(
                content=body_text or "",
                status_code=int(status or 200),
                media_type="application/json",
                headers={"X-Idempotent-Replay": "true"},
            )

        # miss or expired — claim the key before running the handler.
        claimed = False
        if classification == _LOOKUP_MISS:
            claimed = _try_claim(
                db, key, user_id, request.method, request.url.path, request_hash
            )
            if not claimed:
                # A competing request claimed it between our lookup
                # and our insert. Re-classify; the loser responds per
                # whatever state the winner is in now.
                classification, status, body_text = _lookup_row(
                    db, key, user_id, request_hash
                )
                if classification == _LOOKUP_HIT:
                    return Response(
                        content=body_text or "",
                        status_code=int(status or 200),
                        media_type="application/json",
                        headers={"X-Idempotent-Replay": "true"},
                    )
                if classification == _LOOKUP_CONFLICT:
                    return _conflict_response(
                        "idempotency_conflict",
                        "Idempotency-Key already used with a different request body.",
                    )
                # Default: concurrent still-in-flight.
                return _conflict_response(
                    "idempotency_in_progress",
                    "Another request with this Idempotency-Key is in flight. "
                    "Retry in a moment.",
                )
        else:
            # expired — overwrite existing row back to pending.
            claimed = _reclaim_expired(
                db, key, user_id, request.method, request.url.path, request_hash
            )
            if not claimed:
                return _conflict_response(
                    "idempotency_in_progress",
                    "Race while reclaiming an expired Idempotency-Key slot. Retry.",
                )

        # We own the key. Run the handler.
        response = await call_next(request)

        if 200 <= response.status_code < 300:
            # Drain response body to bytes so we can both cache and
            # re-emit it.
            body_chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                body_chunks.append(chunk)
            captured = b"".join(body_chunks)

            _finalize_row(db, key, user_id, response.status_code, captured)

            return Response(
                content=captured,
                status_code=response.status_code,
                media_type=response.media_type,
                headers=dict(response.headers),
            )

        # Non-2xx — release the key so the caller can retry freshly.
        _release_row(db, key, user_id)
        return response
    finally:
        db.close()


def prune_expired_idempotency_keys(db: Session) -> int:
    """Delete rows older than the TTL OR pending rows past the watchdog.

    Called hourly by the scheduler. Returns the number of rows deleted
    so the scheduler can log activity.

    Cutoff is passed as an ISO-8601 string rather than a datetime
    object because SQLite's default datetime adapter (deprecated as of
    Python 3.12) was stripping the UTC offset and producing silent
    zero-match comparisons against naive ISO strings written by
    `_finalize_row`. String-on-string comparison is stable on both
    SQLite and Postgres and matches the column's TEXT storage.
    """
    now = datetime.now(timezone.utc)
    complete_cutoff = (now - timedelta(hours=_TTL_HOURS)).isoformat()
    pending_cutoff = (now - timedelta(seconds=_PENDING_WATCHDOG_SECONDS)).isoformat()

    result1 = db.execute(
        text(
            "DELETE FROM idempotency_keys "
            "WHERE state = 'complete' AND created_at < :cutoff"
        ),
        {"cutoff": complete_cutoff},
    )
    # Stuck pending rows — orphaned by a crashed handler.
    result2 = db.execute(
        text(
            "DELETE FROM idempotency_keys "
            "WHERE state = 'pending' AND created_at < :cutoff"
        ),
        {"cutoff": pending_cutoff},
    )
    db.commit()
    try:
        return int((result1.rowcount or 0) + (result2.rowcount or 0))
    except Exception:
        return 0
