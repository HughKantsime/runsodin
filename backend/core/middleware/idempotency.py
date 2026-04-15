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

# Cap on request-body size we're willing to buffer for hashing. Codex
# pass 4 (2026-04-14) flagged that `await request.body()` on large
# upload endpoints (vision models up to 500 MB, print-files up to
# 100 MB, backups up to 100 MB) would double memory pressure by
# holding the full payload in the middleware AND in the route. Also,
# multipart boundaries randomize per-retry, so a raw-byte hash
# produces spurious 409 conflicts on legitimate retries. Both issues
# are fixed by passing through without the middleware for oversized
# or multipart requests.
#
# 1 MB is comfortably above every agent-surface JSON envelope we
# know about (largest projected agent payload is a bulk-job create
# in the tens of KB). Operators who want idempotency on a large
# binary endpoint should use ETag/If-Match or application-level
# dedup instead.
_MAX_REQUEST_BODY_BYTES = 1024 * 1024

# Completed-row TTL. Replays within this window; pruner cleans up.
_TTL_HOURS = 24

# Pending-row watchdog: if a handler crashes without writing the
# response, the key would be wedged until the pruner runs. The window
# must be LONGER than the longest legitimate mutating handler or else
# a slow-running request gets "reclaimed" by a retry and both
# executions produce side effects.
#
# Codex pass 7 (2026-04-15): original value of 90s was too short —
# timelapse edit routes invoke ffmpeg with 300-600s subprocess
# timeouts (modules/archives/routes/projects.py). The reclaim would
# fire while the first handler was still running, defeating the
# whole idempotency contract. The watchdog is now 900s (15 min),
# which covers every mutating handler currently in the tree. Routes
# that ever exceed this should NOT send `Idempotency-Key` — the
# middleware short-circuits cleanly when the header is absent, so
# an opt-out is just "don't include the header."
_PENDING_WATCHDOG_SECONDS = 15 * 60

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_STATE_PENDING = "pending"
_STATE_COMPLETE = "complete"


def _compute_auth_fingerprint(user: Optional[dict]) -> str:
    """Stable authorization fingerprint for a resolved user.

    Codex pass 6 (2026-04-15): replay must invalidate when the user's
    authz-relevant state changes (role demotion, scope reduction, org
    move). This function returns a canonical string that encodes
    those fields; the middleware persists it at claim time and
    compares at replay time.

    Fields included:
      - role: primary RBAC role (admin / operator / viewer).
      - group_id: org membership (org-scoped access checks).
      - _token_scopes: JSON array of token scopes (sorted for
        stability); empty for JWT/cookie sessions, populated for
        per-user API tokens.
      - is_active: always 1 here because `_resolve_user_id` rejects
        inactive users — but included so an explicit `deactivate +
        reactivate within 24h` still shows a different fingerprint
        when combined with other changes.

    Produces `role=X|gid=Y|sc=[...]|act=1`. Empty string for unresolved.
    """
    if not user:
        return ""
    role = user.get("role") or ""
    gid = user.get("group_id")
    gid_s = "" if gid is None else str(int(gid))
    scopes = user.get("_token_scopes") or []
    try:
        # Sort for stable serialization.
        scopes_s = json.dumps(sorted([str(s) for s in scopes]))
    except Exception:
        scopes_s = "[]"
    active = "1" if user.get("is_active") else "0"
    return f"role={role}|gid={gid_s}|sc={scopes_s}|act={active}"


def _resolve_user_id(request: Any, db: Session) -> Optional[int]:
    """Full-auth user-id resolution for idempotency key scoping.

    Codex pass 5 (2026-04-14) flagged that the middleware's own
    auth check was lighter than `get_current_user` — missing the
    token blacklist, MFA-pending / ws-only purpose claims, and
    session-cookie path. Combined with the fact that the perimeter
    `authenticate_request` middleware does NOT validate Authorization
    Bearer tokens at all (it only checks X-API-Key + cookie), a
    revoked Bearer could still serve a cached 2xx.

    This function now replicates every check `get_current_user`
    performs before returning a user_id:
      - JWT decode + blacklist (`token_blacklist.jti`)
      - Reject `ws`, `mfa_pending`, `mfa_setup_required` tokens
      - Active session must still exist for the jti
      - User `is_active`
      - For `odin_` API tokens: token_hash match, not-past expires_at,
        owning user active
      - For the global API_KEY env: constant-time compare
      - For session cookie: same JWT decode + blacklist + purpose
        checks as Bearer

    Returns None on any failure. The middleware then passes through
    without touching the cache — `authenticate_request` rejects
    downstream OR the route's own Depends(get_current_user) rejects,
    depending on deployment.
    """

    def _check_jwt(token: str) -> Optional[int]:
        """JWT → user_id, running every validation get_current_user does."""
        try:
            from core.auth import decode_token
            import jwt as _jwt
            from core.auth import SECRET_KEY, ALGORITHM
            token_data = decode_token(token)
            if not token_data:
                return None
            payload = _jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            # Purpose claims: any of these means the token is not valid
            # for normal routes (matches get_current_user behavior).
            if payload.get("ws") or payload.get("mfa_pending") or payload.get("mfa_setup_required"):
                return None
            # Blacklist check — revoked sessions.
            #
            # Codex pass 7 (2026-04-15): do NOT require `active_sessions`
            # presence. `get_current_user` in core/dependencies.py does
            # NOT require it either (it only tries to update
            # last_seen_at best-effort and tolerates missing rows).
            # Requiring the row here created a split-brain where valid
            # authenticated callers whose session row had been cleaned
            # up would bypass the idempotency cache entirely, opening
            # a duplicate-execution window. Keep the blacklist check
            # (revocation is real auth); skip the active_sessions
            # gate so our auth matches the route's.
            jti = payload.get("jti")
            if jti:
                bl = db.execute(
                    text("SELECT 1 FROM token_blacklist WHERE jti = :jti"),
                    {"jti": jti},
                ).fetchone()
                if bl:
                    return None
            username = token_data.username
            if not username:
                return None
            row = db.execute(
                text("SELECT id, is_active FROM users WHERE username = :u"),
                {"u": username},
            ).fetchone()
            if not row or not row.is_active:
                return None
            return int(row.id)
        except Exception:
            return None

    # Authorization: Bearer
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _check_jwt(auth[7:])

    # Session cookie — mirrors authenticate_request's cookie path.
    try:
        cookies = getattr(request, "cookies", None)
        if cookies is None:
            cookies = {}
        session_cookie = cookies.get("session") if hasattr(cookies, "get") else None
    except Exception:
        session_cookie = None
    if session_cookie:
        uid = _check_jwt(session_cookie)
        if uid is not None:
            return uid

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


def _resolve_user_context(request: Any, db: Session) -> Optional[dict]:
    """Full user context including role + scopes for fingerprinting.

    Returns a dict with at least `id`, `role`, `group_id`, `is_active`,
    `_token_scopes`. None if unresolved or fails any of the checks in
    `_resolve_user_id` — this helper is a superset.

    Callers who only need the ID can use `_resolve_user_id`; callers
    who need the authz fingerprint for cache consistency use this.
    """
    uid = _resolve_user_id(request, db)
    if uid is None:
        return None

    row = db.execute(
        text(
            "SELECT id, username, role, group_id, is_active "
            "FROM users WHERE id = :id"
        ),
        {"id": uid},
    ).fetchone()
    if not row or not row.is_active:
        return None

    ctx: dict = {
        "id": int(row.id),
        "username": row.username,
        "role": row.role,
        "group_id": row.group_id,
        "is_active": bool(row.is_active),
        "_token_scopes": [],
    }

    # If the request used a per-user token, attach its scopes so the
    # fingerprint captures scope revocation.
    api_key_header = request.headers.get("X-API-Key", "")
    if api_key_header.startswith("odin_"):
        try:
            from core.auth import verify_password
            prefix = api_key_header[:10]
            candidates = db.execute(
                text(
                    "SELECT token_hash, scopes FROM api_tokens "
                    "WHERE token_prefix = :p AND user_id = :u"
                ),
                {"p": prefix, "u": uid},
            ).fetchall()
            for candidate in candidates:
                if verify_password(api_key_header, candidate.token_hash):
                    try:
                        ctx["_token_scopes"] = (
                            json.loads(candidate.scopes) if candidate.scopes else []
                        )
                    except Exception:
                        ctx["_token_scopes"] = []
                    break
        except Exception:
            pass

    return ctx


def _canonicalize_query_string(query: str) -> str:
    """Sort query params by (key, value) for a stable hash input.

    Codex pass 2 (2026-04-14): several ODIN mutating routes carry
    their semantic parameters in the query string rather than the
    body (e.g. `POST /vision/models?name=X&detection_type=Y` with a
    file-upload body). If the hash ignored query params, two
    different operations with the same key and body would be
    indistinguishable and replay would serve the wrong result.

    Preserves repeated keys (`?tag=a&tag=b`) by sorting with a
    stable (key, value) tuple ordering.
    """
    if not query:
        return ""
    from urllib.parse import parse_qsl, urlencode
    pairs = parse_qsl(query, keep_blank_values=True)
    pairs.sort()
    return urlencode(pairs)


def _compute_request_hash(method: str, path: str, body: bytes, query: str = "") -> str:
    """Deterministic hash over the request identity + body.

    Includes: method, path, canonicalized query string, canonicalized
    body. JSON bodies are canonicalized (sorted keys) before hashing
    so `{"a":1,"b":2}` and `{"b":2,"a":1}` hash the same and don't
    false-409 the client.

    Query string is canonicalized (sorted pairs) so `?b=2&a=1` and
    `?a=1&b=2` hash the same but `?name=foo` and `?name=bar` differ.
    """
    payload_bytes = body or b""
    if payload_bytes:
        try:
            parsed = json.loads(payload_bytes.decode("utf-8"))
            payload_bytes = json.dumps(parsed, sort_keys=True).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Non-JSON body (e.g. multipart upload) — hash verbatim.
            pass

    canonical_query = _canonicalize_query_string(query or "")

    h = hashlib.sha256()
    h.update(method.encode("ascii"))
    h.update(b"\x00")
    h.update(path.encode("utf-8"))
    h.update(b"\x00")
    h.update(canonical_query.encode("utf-8"))
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
_LOOKUP_STUCK_PENDING = "stuck_pending"
_LOOKUP_CONFLICT = "conflict"
_LOOKUP_HIT = "hit"
_LOOKUP_EXPIRED = "expired"


def _lookup_row(
    db: Session, key: str, user_id: int, request_hash: str
) -> tuple[str, Optional[int], Optional[str], Optional[str], Optional[str]]:
    """Look up the cache row and classify its state.

    Returns (classification, status, body, created_at_str, auth_fingerprint).
    The fifth element is the stored authz fingerprint from claim time;
    the middleware compares it against the caller's current fingerprint
    before serving a replay (codex pass 6 — closes role/scope/org
    revocation gap).
    """
    row = db.execute(
        text(
            "SELECT request_hash, state, response_status, response_body, "
            "created_at, updated_at, auth_fingerprint "
            "FROM idempotency_keys "
            "WHERE key = :k AND user_id = :u"
        ),
        {"k": key, "u": user_id},
    ).fetchone()
    if not row:
        return _LOOKUP_MISS, None, None, None, None

    state = row.state
    now = datetime.now(timezone.utc)
    created_raw = row.created_at
    fp = row.auth_fingerprint

    if state == _STATE_PENDING:
        created = _parse_created_at(created_raw)
        if created is None:
            return _LOOKUP_MISS, None, None, None, None
        if now - created > timedelta(seconds=_PENDING_WATCHDOG_SECONDS):
            log.debug(
                "idempotency: pending row older than %ss — stuck_pending",
                _PENDING_WATCHDOG_SECONDS,
            )
            return _LOOKUP_STUCK_PENDING, None, None, str(created_raw), fp
        if row.request_hash != request_hash:
            return _LOOKUP_CONFLICT, None, None, None, None
        return _LOOKUP_PENDING, None, None, None, fp

    # state == complete
    if row.request_hash != request_hash:
        return _LOOKUP_CONFLICT, None, None, None, None

    created = _parse_created_at(created_raw)
    if created is None:
        return _LOOKUP_MISS, None, None, None, None
    if now - created > timedelta(hours=_TTL_HOURS):
        return _LOOKUP_EXPIRED, None, None, str(created_raw), fp

    return _LOOKUP_HIT, int(row.response_status), row.response_body, str(created_raw), fp


_SCHEMA_READY_CACHE: dict = {"ready": False, "checked_at": 0.0}


def _idempotency_schema_ready() -> bool:
    """Check (and cache) whether migration 005's table exists.

    Codex pass 8 (2026-04-15): the middleware is registered
    unconditionally at app startup, but a code-first deploy or a
    partial rollback can leave the `idempotency_keys` table missing.
    Hitting it on the request path would 500 every mutating request
    that carries Idempotency-Key. This helper probes existence once
    and caches the result for 60s so the hot path stays cheap.

    Returns True if the table exists, False otherwise. Errors
    during probe (e.g. DB unreachable) also return False — the
    middleware degrades to pass-through.
    """
    import time
    now = time.monotonic()
    if _SCHEMA_READY_CACHE["ready"] and (now - _SCHEMA_READY_CACHE["checked_at"]) < 60:
        return True

    from core.db import SessionLocal
    db = SessionLocal()
    try:
        # Portable existence probe: SELECT 1 LIMIT 0. Succeeds on
        # SQLite AND Postgres if the table exists, raises if not.
        db.execute(text("SELECT 1 FROM idempotency_keys LIMIT 0"))
        _SCHEMA_READY_CACHE["ready"] = True
        _SCHEMA_READY_CACHE["checked_at"] = now
        return True
    except Exception:
        _SCHEMA_READY_CACHE["ready"] = False
        _SCHEMA_READY_CACHE["checked_at"] = now
        return False
    finally:
        db.close()


def _now_iso() -> str:
    """Canonical timestamp format used for every row in this table.

    Codex pass 2 (2026-04-14): keeps all timestamps in a single ISO-8601
    format so lexical comparison is sound across SQLite and Postgres.
    Never return the DB-native `CURRENT_TIMESTAMP` shape.
    """
    return datetime.now(timezone.utc).isoformat()


def _try_claim(
    db: Session,
    key: str,
    user_id: int,
    method: str,
    path: str,
    request_hash: str,
    auth_fingerprint: str = "",
) -> bool:
    """Atomically claim the key by inserting a pending row.

    Returns True if we own the key (handler should run). False if the
    row already existed (caller re-reads to see why — pending, hit,
    conflict, or expired).

    The insert is guarded by the PK; concurrent attempts resolve
    deterministically: exactly one INSERT succeeds, the rest raise
    IntegrityError. Stuck-pending rows are handled separately by the
    caller (treats them as miss and goes through `_reclaim_expired`
    which is a compare-and-set, single-winner).

    `auth_fingerprint` is the stable encoding of the caller's authz-
    relevant state at claim time (see `_compute_auth_fingerprint`).
    Stored verbatim so replay can compare against the current user's
    fingerprint and invalidate on role/scope/org changes.
    """
    ts = _now_iso()
    try:
        db.execute(
            text(
                "INSERT INTO idempotency_keys "
                "(key, user_id, method, path, request_hash, auth_fingerprint, "
                " state, response_status, response_body, created_at, updated_at) "
                "VALUES (:k, :u, :m, :p, :h, :af, 'pending', 0, '', :ts, :ts)"
            ),
            {
                "k": key,
                "u": user_id,
                "m": method,
                "p": path,
                "h": request_hash,
                "af": auth_fingerprint,
                "ts": ts,
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
    prior_created_at: str,
    auth_fingerprint: str = "",
) -> bool:
    """Compare-and-set claim of a stuck-pending or TTL-expired row.

    Codex pass 2 (2026-04-14) flagged a race: the prior implementation
    did an unconditional UPDATE and returned True unconditionally. Two
    concurrent retriers would both "succeed" and both execute the
    handler, duplicating side effects on the recovery path.

    Fix: guard the UPDATE on the caller-observed `created_at`. The
    caller classifies the row (sees `created_at = T`) and passes T to
    this function. The UPDATE runs only if the row's current
    created_at still matches T — i.e. no other writer has touched it
    since. If another reclaimer won, the row now has a newer
    created_at and this UPDATE affects 0 rows; we return False and
    the caller's outer loop re-reads the classifier (the other
    reclaimer's work is now visible as pending or complete).

    Returns True iff exactly one row was updated by this call (we own
    the key). False otherwise (another reclaimer won the race).
    """
    now_iso = _now_iso()
    try:
        result = db.execute(
            text(
                "UPDATE idempotency_keys "
                "SET method = :m, path = :p, request_hash = :h, "
                "    auth_fingerprint = :af, "
                "    state = 'pending', response_status = 0, "
                "    response_body = '', "
                "    created_at = :now, updated_at = :now "
                "WHERE key = :k AND user_id = :u "
                "  AND created_at = :prior"
            ),
            {
                "k": key, "u": user_id, "m": method, "p": path, "h": request_hash,
                "af": auth_fingerprint,
                "now": now_iso, "prior": prior_created_at,
            },
        )
        db.commit()
        rowcount = int(getattr(result, "rowcount", 0) or 0)
        return rowcount == 1
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
    """UPDATE the pending row to complete with the real response.

    Codex pass 2 (2026-04-14): fail loud — raise if the UPDATE does
    not affect exactly one row.

    Codex pass 5 (2026-04-14): refuse to cache anything that can't be
    faithfully replayed. If the response body exceeds the cap, the
    caller should have invoked `_release_row` instead; `_finalize_row`
    only accepts a body it can store verbatim.
    """
    try:
        body_text = response_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IdempotencyFinalizeError(
            f"idempotency finalize: response body not UTF-8 "
            f"for key={key!r} user_id={user_id}: {exc}"
        ) from exc

    if len(response_body) > _MAX_BODY_BYTES:
        raise IdempotencyFinalizeError(
            f"idempotency finalize: response body {len(response_body)} bytes "
            f"exceeds {_MAX_BODY_BYTES}-byte cap. Caller should release the "
            f"pending row instead of caching a truncated response."
        )

    try:
        result = db.execute(
            text(
                "UPDATE idempotency_keys "
                "SET state = 'complete', "
                "    response_status = :s, "
                "    response_body = :b, "
                "    updated_at = :now "
                "WHERE key = :k AND user_id = :u "
                "  AND state = 'pending'"
            ),
            {
                "k": key, "u": user_id,
                "s": response_status, "b": body_text,
                "now": _now_iso(),
            },
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise IdempotencyFinalizeError(
            f"idempotency finalize DB error for key={key!r} user_id={user_id}: {exc}"
        ) from exc

    rowcount = int(getattr(result, "rowcount", 0) or 0)
    if rowcount != 1:
        raise IdempotencyFinalizeError(
            f"idempotency finalize expected 1 row updated, got {rowcount} "
            f"for key={key!r} user_id={user_id}. Row may have been pruned "
            f"mid-flight or state was not 'pending'."
        )


# Only these response headers are safe to drop on replay. Any
# additional header (Set-Cookie, Content-Disposition, Location, ETag,
# Last-Modified, Cache-Control, custom X-*, etc.) carries semantics
# the replay would silently lose. The allowlist approach fails closed:
# new header types default to "not cacheable" without needing a code
# change.
_CACHEABLE_RESPONSE_HEADERS: frozenset[str] = frozenset({
    "content-type",
    "content-length",
})


def _response_is_cacheable(response: Any, body: bytes) -> tuple[bool, str]:
    """Decide whether a 2xx response can be faithfully replayed.

    Returns (cacheable, reason). When False, caller MUST release the
    pending row so a retry executes the handler freshly rather than
    serving a partial/incorrect replay.

    Refuse to cache if any of:
      - Body exceeds `_MAX_BODY_BYTES`.
      - Body is not UTF-8.
      - Response has any header outside `_CACHEABLE_RESPONSE_HEADERS`.
        Codex passes 5 & 6 (2026-04-14/15) surfaced multiple examples
        of header-drop hazards: Set-Cookie (session desync),
        Content-Disposition (file download filename lost), Location
        (redirect semantics lost), ETag / Last-Modified (conditional-
        request contracts broken). Allowlist is stricter than an
        explicit deny list, so new header types fail safe without a
        code change.
    """
    if len(body) > _MAX_BODY_BYTES:
        return False, f"body {len(body)} bytes exceeds {_MAX_BODY_BYTES}-byte cap"

    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return False, "response body is not UTF-8"

    try:
        raw = getattr(response, "raw_headers", None) or []
        for name, _value in raw:
            try:
                name_s = (
                    name.decode("ascii") if isinstance(name, bytes) else str(name)
                ).lower()
            except Exception:
                return False, "could not decode a response header name"
            if name_s not in _CACHEABLE_RESPONSE_HEADERS:
                return False, f"response carries non-cacheable header: {name_s!r}"
    except Exception:
        return False, "could not inspect response headers"

    return True, ""


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


def _release_row_force(db: Session, key: str, user_id: int) -> None:
    """Delete ANY row (pending or complete) for this (key, user_id).

    Used when authz-fingerprint drift invalidates a cached hit
    (codex pass 6, 2026-04-15). The replay would be unsafe; wipe the
    row so a fresh claim runs through current authz.
    """
    try:
        db.execute(
            text(
                "DELETE FROM idempotency_keys "
                "WHERE key = :k AND user_id = :u"
            ),
            {"k": key, "u": user_id},
        )
        db.commit()
    except Exception:
        db.rollback()


def _conflict_response(code: str, detail: str):
    """Build the 409 envelope for in-progress / conflict cases.

    Dual-shape body (codex pass 3, 2026-04-14): top-level `detail`
    for legacy client compat, plus `error.{code,detail,retriable}`
    for agents.
    """
    from fastapi.responses import Response  # noqa: WPS433
    return Response(
        content=json.dumps({
            "detail": detail,
            "error": {
                "code": code,
                "detail": detail,
                "retriable": code == "idempotency_in_progress",
            },
        }),
        status_code=409,
        media_type="application/json",
    )


def _concurrent_loser_response(
    db: Session, key: str, user_id: int, request_hash: str, current_fp: str = ""
):
    """Build the response for a request that lost a concurrent claim.

    Re-classifies the row and picks the right 409 / replay for the
    winner's current state:
    - HIT with matching authz fingerprint: serve the cached response.
    - HIT with mismatched fingerprint: 409 in_progress (authz changed
      between our claim attempt and the winner's success; the loser
      should retry freshly to get current authz enforcement).
    - CONFLICT: winner has a different request hash → 409.
    - anything else (pending / stuck / expired): 409 in_progress.
    """
    from fastapi.responses import Response  # noqa: WPS433
    classification, status, body_text, _, stored_fp = _lookup_row(
        db, key, user_id, request_hash
    )
    if classification == _LOOKUP_HIT:
        if stored_fp is not None and stored_fp != current_fp:
            # Winner ran under a different authz context — don't
            # replay that to the current user.
            return _conflict_response(
                "idempotency_in_progress",
                "Another request under a different authorization context holds this key. Retry.",
            )
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
    return _conflict_response(
        "idempotency_in_progress",
        "Another request with this Idempotency-Key is in flight. "
        "Retry in a moment.",
    )


class IdempotencyFinalizeError(RuntimeError):
    """Raised when _finalize_row cannot persist the completed row.

    Codex pass 2 (2026-04-14): silent failure here left the row in
    `pending` state (or absent), which meant the hourly pruner could
    delete it and a later retry would execute the mutation a second
    time. Surfacing this as an exception lets the middleware convert
    a successful handler response into a 500 + clear operator message
    rather than returning 2xx that cannot be replayed.
    """


async def idempotency_middleware(request: Any, call_next: Callable):
    """FastAPI HTTP middleware: cache-by-Idempotency-Key for mutators.

    Wired into the app factory via `app.middleware("http")`. Must be
    registered INSIDE the auth middleware so cached-replay requests
    still pass auth validation (token expiry, active user, scope).

    Codex pass 8 (2026-04-15): if migration 005 hasn't applied (code-
    first deploy, stale worker, partial rollback), the cache table
    doesn't exist. The middleware degrades to pass-through rather
    than 500ing every mutating request — the retry contract is lost
    for that deploy window, but the service stays up. `_schema_ready`
    is cached process-local for 60s to keep the hot path cheap.
    """
    from fastapi.responses import Response  # noqa: WPS433

    # Fast-path: non-mutating method. No work.
    if request.method not in _MUTATING_METHODS:
        return await call_next(request)

    key = request.headers.get("Idempotency-Key")
    if not key:
        return await call_next(request)

    if not _idempotency_schema_ready():
        # Migration 005 hasn't applied yet. Degrade cleanly — no
        # caching, but the request still executes.
        log.warning(
            "idempotency_keys table missing — middleware disabled until "
            "migration 005 applies. Retry contract not active."
        )
        return await call_next(request)

    # Codex pass 9 (2026-04-15): do NOT silently disable idempotency
    # on multipart or oversized requests. Silently passing through
    # turned agent retries on /vision/models, /print-files/upload,
    # /backups/restore into duplicate-execution holes. Return an
    # explicit 415-ish error so the client knows the retry contract
    # is unavailable and can fall back to application-level dedup.
    #
    # Operators on those routes should rely on ETag/If-Match or
    # content-hash deduplication at the application layer.
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/"):
        return Response(
            content=json.dumps({
                "detail": (
                    "Idempotency-Key is not supported for multipart/form-data "
                    "uploads. Use ETag/If-Match or content-hash dedup instead."
                ),
                "error": {
                    "code": "idempotency_unsupported",
                    "detail": "Idempotency-Key not supported for multipart uploads.",
                    "retriable": False,
                },
            }),
            status_code=415,
            media_type="application/json",
        )
    content_length_str = request.headers.get("content-length") or ""
    if content_length_str.isdigit() and int(content_length_str) > _MAX_REQUEST_BODY_BYTES:
        return Response(
            content=json.dumps({
                "detail": (
                    f"Idempotency-Key is not supported for requests larger "
                    f"than {_MAX_REQUEST_BODY_BYTES} bytes. "
                    f"Use application-level dedup for large uploads."
                ),
                "error": {
                    "code": "idempotency_unsupported",
                    "detail": f"Idempotency-Key not supported for >{_MAX_REQUEST_BODY_BYTES}-byte requests.",
                    "retriable": False,
                },
            }),
            status_code=413,
            media_type="application/json",
        )

    # Codex pass 11 (2026-04-15): refuse Idempotency-Key on chunked
    # requests that don't declare a Content-Length. Without a bounded
    # length header, `await request.body()` would buffer the full
    # payload in memory before we could enforce the cap — a caller
    # could force the app to hold 100+ MB per request and only then
    # see a 413. Require a declared, bounded Content-Length.
    if not content_length_str.isdigit():
        return Response(
            content=json.dumps({
                "detail": (
                    "Idempotency-Key requires a declared Content-Length. "
                    "Chunked transfer encoding without Content-Length is "
                    "not supported; re-send with Content-Length set."
                ),
                "error": {
                    "code": "idempotency_unsupported",
                    "detail": "Idempotency-Key requires a declared Content-Length header.",
                    "retriable": False,
                },
            }),
            status_code=411,  # Length Required
            media_type="application/json",
        )

    # Body read is now safe — Content-Length is bounded and below cap.
    body = await request.body()

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = _receive  # type: ignore[assignment]

    from core.db import SessionLocal
    db = SessionLocal()
    try:
        user_ctx = _resolve_user_context(request, db)
        if user_ctx is None:
            # Anonymous / unresolvable / expired — pass through; auth
            # will reject downstream and we don't want to cache under
            # a null key.
            return await call_next(request)

        user_id = user_ctx["id"]
        current_fp = _compute_auth_fingerprint(user_ctx)

        request_hash = _compute_request_hash(
            request.method,
            request.url.path,
            body,
            query=(request.url.query or ""),
        )

        classification, status, body_text, created_at_str, stored_fp = _lookup_row(
            db, key, user_id, request_hash
        )

        # Codex pass 10 (2026-04-15): authz drift must NOT re-execute.
        # Earlier fix (pass 6) deleted the completed row and treated
        # the request as MISS so the handler ran under current authz.
        # But that reopens duplicate-execution risk: the original
        # mutation already took effect, and the fresh handler run
        # might be accepted by the route's own Depends() under the
        # new authz state — producing a second job/order/write.
        # Idempotency's strongest guarantee is "at-most-once for this
        # key"; that guarantee must survive authz drift.
        #
        # New behavior: keep the completed row, refuse to replay under
        # the new context, refuse to re-execute either. Return 409
        # authz_changed — client must mint a FRESH key for a new
        # request under current authz. This is retriable only from
        # the caller's perspective (with a different key).
        if classification == _LOOKUP_HIT and stored_fp is not None and stored_fp != current_fp:
            log.info(
                "idempotency: authz fingerprint changed for %s %s "
                "(user_id=%s); refusing replay AND re-execution",
                request.method, request.url.path, user_id,
            )
            return _conflict_response(
                "idempotency_authz_changed",
                "Authorization context has changed since this Idempotency-Key "
                "was used for a completed request. Mint a new Idempotency-Key "
                "for any fresh request under the current authorization.",
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

        # miss / stuck_pending / expired — claim the key before the handler.
        claimed = False
        if classification == _LOOKUP_MISS:
            claimed = _try_claim(
                db, key, user_id,
                request.method, request.url.path, request_hash,
                auth_fingerprint=current_fp,
            )
            if not claimed:
                # Race lost at INSERT time. Re-read to see who won.
                return _concurrent_loser_response(
                    db, key, user_id, request_hash, current_fp,
                )
        else:
            if created_at_str is None:
                return _conflict_response(
                    "idempotency_in_progress",
                    "Transient corruption on Idempotency-Key slot. Retry.",
                )
            claimed = _reclaim_expired(
                db, key, user_id,
                request.method, request.url.path, request_hash,
                prior_created_at=created_at_str,
                auth_fingerprint=current_fp,
            )
            if not claimed:
                return _concurrent_loser_response(
                    db, key, user_id, request_hash, current_fp,
                )

        # We own the key. Run the handler.
        # Codex pass 3 (2026-04-14): if call_next raises, we MUST
        # release the pending row or retries get 409 in_progress
        # until the 90s watchdog expires — which pressures callers
        # into switching keys (the anti-pattern we're trying to
        # prevent). Release + re-raise preserves the safe-retry
        # contract: the client can retry with the same key and
        # either succeed or get a clean error.
        try:
            response = await call_next(request)
        except BaseException:
            _release_row(db, key, user_id)
            raise

        if 200 <= response.status_code < 300:
            # Drain response body to bytes so we can both cache and
            # re-emit it. Starlette's StreamingResponse exposes
            # `body_iterator`; plain Response uses `.body` (already
            # bytes in memory).
            if hasattr(response, "body_iterator"):
                body_chunks: list[bytes] = []
                async for chunk in response.body_iterator:
                    body_chunks.append(chunk)
                captured = b"".join(body_chunks)
            else:
                captured = bytes(getattr(response, "body", b"") or b"")

            # Codex pass 5 (2026-04-14): not every 2xx is safely
            # replayable. Cookies, oversized bodies, and non-UTF-8
            # payloads must be released instead of cached — otherwise
            # a retry sees an inauthentic replay.
            cacheable, reason = _response_is_cacheable(response, captured)
            if not cacheable:
                log.info(
                    "idempotency: skipping cache for %s %s — %s",
                    request.method, request.url.path, reason,
                )
                _release_row(db, key, user_id)
                return Response(
                    content=captured,
                    status_code=response.status_code,
                    media_type=response.media_type,
                    headers=dict(response.headers),
                )

            try:
                _finalize_row(db, key, user_id, response.status_code, captured)
            except IdempotencyFinalizeError as exc:
                # The handler succeeded but the cache write didn't land
                # exactly once. Returning the 2xx would risk a duplicate
                # execution on retry (pruner deletes the row, next
                # request hits miss, runs the mutation again). Fail loud.
                log.error("idempotency finalize failed: %s", exc)
                _detail = (
                    "Request succeeded but idempotency cache could not be "
                    "persisted. Check server logs; the operation may or may "
                    "not have taken effect — verify before retrying."
                )
                return Response(
                    content=json.dumps({
                        "detail": _detail,
                        "error": {
                            "code": "internal_error",
                            "detail": _detail,
                            "retriable": False,
                        },
                    }),
                    status_code=500,
                    media_type="application/json",
                )

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
