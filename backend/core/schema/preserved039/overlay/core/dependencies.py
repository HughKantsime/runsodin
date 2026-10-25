"""
O.D.I.N. — Core auth/request dependencies.

Provides the get_current_user FastAPI dependency (resolves the caller from
cookie, JWT Bearer token, or API key) and the log_audit utility.

Extracted from deps.py as part of the modular architecture refactor.
Old import path (from deps import get_current_user) continues to work via re-exports in deps.py.
"""

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

import core.auth as auth_module
from core.auth import decode_token, verify_password
from core.api_token_policy import parse_token_scopes
from core.audit import log_audit
from core.db import get_db

log = logging.getLogger("odin.api")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


# ---------------------------------------------------------------------------
# Last-seen batching (R6 from 2026-04-12 adversarial review)
# ---------------------------------------------------------------------------
#
# Previously get_current_user ran a DB write + commit on every authenticated
# request to update active_sessions.last_seen_at. On SQLite, this turned
# every read-heavy dashboard page into a writer — with 10+ concurrent
# pollers we'd serialize behind the single SQLite writer, causing auth
# latency spikes and SQLITE_BUSY on unrelated reads.
#
# Fix: two gates in front of the write.
#   1. In-process: skip if this worker refreshed the jti within the interval
#      (no SQL at all).
#   2. Stored value: read last_seen_at and write only when it is NULL or older
#      than the interval. The UPDATE repeats that condition, so racing workers
#      cannot both rewrite and a newer value is never moved back. A fresh row
#      costs a read and no SQLite write lock, across workers.
# The interval is ODIN_SESSION_LAST_SEEN_INTERVAL_SECONDS (default 60, 1..86400).
# Consumers: the session lists (display/order), the privacy export, and the
# retention cleanup that drops sessions idle for 48 h; none needs finer than a
# minute, and the 1-day maximum stays well inside the 48 h idle cutoff.
# api_tokens.last_used_at is throttled the same way.
import threading as _threading

_DEFAULT_LAST_SEEN_INTERVAL_SECONDS = 60
_MAX_LAST_SEEN_INTERVAL_SECONDS = 86400


def _last_seen_interval_from_env(environ) -> int:
    """Validated refresh interval for session/token bookkeeping; invalid values fail loudly."""
    raw = environ.get("ODIN_SESSION_LAST_SEEN_INTERVAL_SECONDS")
    if raw is None or raw == "":
        return _DEFAULT_LAST_SEEN_INTERVAL_SECONDS
    if not raw.isdigit() or not 1 <= int(raw) <= _MAX_LAST_SEEN_INTERVAL_SECONDS:
        raise ValueError("ODIN_SESSION_LAST_SEEN_INTERVAL_SECONDS must be an integer from 1 to 86400")
    return int(raw)


_LAST_SEEN_MIN_INTERVAL_SECONDS = _last_seen_interval_from_env(os.environ)
_last_seen_cache: dict[str, float] = {}
_last_seen_lock = _threading.Lock()


def _should_write_last_seen(jti: str) -> bool:
    """Return True if we should write last_seen_at for this jti now.

    Thread-safe. Updates the cache as a side effect when returning True,
    so two concurrent callers don't both decide to write.
    """
    if not jti:
        return False
    now = datetime.now(timezone.utc).timestamp()
    with _last_seen_lock:
        prev = _last_seen_cache.get(jti)
        if prev is None or now - prev >= _LAST_SEEN_MIN_INTERVAL_SECONDS:
            _last_seen_cache[jti] = now
            return True
        return False


def _forget_last_seen(jti: str) -> None:
    """Drop a jti from the cache — call this on logout so the next login's
    first request immediately writes last_seen_at instead of waiting for
    the interval to roll over."""
    if not jti:
        return
    with _last_seen_lock:
        _last_seen_cache.pop(jti, None)


def _stale_cutoff(now):
    from datetime import timedelta
    return now - timedelta(seconds=_LAST_SEEN_MIN_INTERVAL_SECONDS)


def _is_fresh(stored, cutoff) -> bool:
    if stored is None:
        return False
    try:
        from core.db_compat import as_utc_datetime
        return as_utc_datetime(stored) >= cutoff
    except (TypeError, ValueError, OverflowError):
        return False  # unreadable stored value: refresh it


def _touch_session_last_seen(db: Session, jti: str, now=None) -> bool:
    """Refresh active_sessions.last_seen_at only if the stored value is stale.

    Returns True when a row was rewritten (and committed)."""
    now = now or datetime.now(timezone.utc)
    cutoff = _stale_cutoff(now)
    stored = db.execute(text("SELECT last_seen_at FROM active_sessions WHERE token_jti = :jti"),
                        {"jti": jti}).fetchone()
    if stored is None or _is_fresh(stored[0], cutoff):
        return False
    updated = db.execute(
        text("UPDATE active_sessions SET last_seen_at = :now WHERE token_jti = :jti "
             "AND (last_seen_at IS NULL OR last_seen_at < :cutoff)"),
        {"now": now, "jti": jti, "cutoff": cutoff},
    ).rowcount
    db.commit()
    return bool(updated)


def token_predates_account(payload, row) -> bool:
    """True when a JWT was issued before the account it names was created.

    Usernames can be reused after a hard delete; a token minted for the old
    account must never authorize the replacement. One second of grace covers
    second-resolution created_at timestamps. Tokens without iat predate this
    check and rely on jti revocation at deletion.
    """
    issued = payload.get("iat") if isinstance(payload, dict) else None
    created = dict(row._mapping).get("created_at") if row is not None else None
    if not isinstance(issued, (int, float)) or created is None:
        return False
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return issued + 1 < created.timestamp()


from core.credential_write_guard import install_credential_write_guard, mark_noninteractive_principal

# Fail-closed backstop for credential routes: see core/credential_write_guard.py.
install_credential_write_guard()


def _principal(
    row,
    auth_kind: str,
    *,
    token_id: int | None = None,
    token_scopes: list[str] | None = None,
) -> Optional[dict]:
    """Return an active principal with explicit authentication provenance."""
    if row is None:
        return None
    principal = dict(row._mapping)
    if not bool(principal.get("is_active")):
        return None
    principal["_auth_kind"] = auth_kind
    if token_id is not None:
        principal["_token_id"] = token_id
    if token_scopes is not None:
        principal["_token_scopes"] = token_scopes
    return principal


def validate_access_token(token: str, db: Session) -> Optional[dict]:
    """Validate a JWT as a full access token and return the user dict.

    Rejects:
      - Malformed / expired / bad-signature tokens
      - Blacklisted tokens (revoked on logout)
      - Special-purpose tokens: ws-only, mfa_pending, mfa_setup_required

    Returns the user dict (from users table) on success, None otherwise.

    This helper exists so non-standard auth carriers (e.g. query-string tokens
    used by <video> / <img> tags that can't send Authorization headers) can
    still enforce the full access-token semantics rather than shortcut-loading
    a user straight from `sub`. Use this instead of calling `decode_token()`
    directly anywhere you are authenticating a request.
    """
    if not token:
        return None
    token_data = decode_token(token)
    if not token_data:
        return None
    import jwt as _jwt
    try:
        payload = _jwt.decode(
            token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM]
        )
    except Exception:
        return None
    # Reject special-purpose tokens (ws, mfa_pending, mfa_setup_required)
    if payload.get("ws") or payload.get("mfa_pending") or payload.get("mfa_setup_required"):
        return None
    # Reject blacklisted tokens (revoked sessions)
    jti = payload.get("jti")
    if jti:
        blacklisted = db.execute(
            text("SELECT 1 FROM token_blacklist WHERE jti = :jti"),
            {"jti": jti},
        ).fetchone()
        if blacklisted:
            return None
    user = db.execute(
        text("SELECT * FROM users WHERE username = :username"),
        {"username": token_data.username},
    ).fetchone()
    if token_predates_account(payload, user):
        return None
    return _principal(user, "bearer_jwt")


def validate_perimeter_access_token(token: str, db: Session) -> Optional[dict]:
    """Accept only the access JWT shape minted by core.auth.create_access_token.

    Login (including OIDC and biometric refresh) issues no type/purpose marker.
    An exact claim set prevents tokens for other purposes from acquiring full
    perimeter authority. Keep legacy route-carrier validation separate.
    """
    import jwt
    try:
        payload = jwt.decode(
            token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM],
            options={"require": ["sub", "role", "exp", "iat", "jti"]},
        )
    except jwt.PyJWTError:
        return None
    if set(payload) != {"sub", "role", "exp", "iat", "jti"}:
        return None
    if (type(payload["exp"]) is not int or type(payload["iat"]) is not int
            or payload["iat"] < 0 or payload["exp"] <= payload["iat"]
            or not isinstance(payload["jti"], str) or not payload["jti"].strip()
            or not isinstance(payload["sub"], str) or not payload["sub"].strip()
            or payload["role"] not in ("admin", "operator", "viewer")):
        return None
    return validate_access_token(token, db)


def resolve_user_api_token(api_key: str, db: Session) -> Optional[dict]:
    """Principal for a per-user scoped API token (odin_xxx), or None.

    Shared by the X-API-Key header path and carriers that cannot send
    headers (e.g. an <img> stream URL); the caller binds the actor.
    """
    if not isinstance(api_key, str) or not api_key.startswith("odin_"):
        return None
    prefix = api_key[:10]
    candidates = db.execute(
        text("SELECT * FROM api_tokens WHERE token_prefix = :prefix"),
        {"prefix": prefix},
    ).fetchall()
    # Constant-time multi-entry compare: verify_password() runs for
    # every candidate, and we only commit to a match AFTER iterating
    # the full list. Folding the expiry check into the per-candidate
    # `usable` flag means an expired token still consumes the same
    # bcrypt time as a valid one. Without this, the first-prefix-
    # match-wins shape made it possible to map prefix density across
    # the token store from the outside via response timing.
    #
    # Historical (v1.9.1 prod incident 2026-04-16): the expiry parse
    # block previously imported `dateutil.parser.parse` which isn't
    # in requirements.txt. The ModuleNotFoundError raised on EVERY
    # scoped-token auth. Switched to stdlib `datetime.fromisoformat`.
    matched_candidate = None
    for candidate in candidates:
        byte_match = verify_password(api_key, candidate.token_hash)
        expired = False
        if candidate.expires_at:
            try:
                raw = candidate.expires_at
                exp = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                expired = exp <= datetime.now(timezone.utc)
            except Exception:
                # Invalid stored expiry cannot establish usable credentials.
                expired = True
        usable = not expired
        if byte_match and usable and matched_candidate is None:
            matched_candidate = candidate
    if matched_candidate is None:
        return None
    candidate = matched_candidate
    # Update last_used_at — best effort. This is telemetry,
    # not part of the auth decision. If it races with another
    # writer (idempotency middleware, another auth call) and
    # SQLite returns `database is locked`, we must NOT fail
    # the auth — the agent would see a 500 on every scoped-
    # token call under any concurrent write load, which is
    # exactly what the v1.9.1 sweep 2026-04-16 surfaced.
    # Throttled like last_seen_at: a fresh stored value costs no write.
    try:
        now = datetime.now(timezone.utc)
        cutoff = _stale_cutoff(now)
        if not _is_fresh(candidate.last_used_at, cutoff):
            db.execute(
                text("UPDATE api_tokens SET last_used_at = :now WHERE id = :id "
                     "AND (last_used_at IS NULL OR last_used_at < :cutoff)"),
                {"now": now, "id": candidate.id, "cutoff": cutoff},
            )
            db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        log.debug(
            "Best-effort api_tokens.last_used_at update failed "
            "(likely SQLite WAL contention): %s", exc,
        )
    # Fetch the user
    user = db.execute(
        text("SELECT * FROM users WHERE id = :id"),
        {"id": candidate.user_id},
    ).fetchone()
    scopes = parse_token_scopes(candidate.scopes)
    if scopes is None or not user:
        return None
    # Combined read:cameras tokens predate the camera-only rule and are revoked.
    from core.api_token_policy import is_legacy_camera_combination
    if is_legacy_camera_combination(scopes):
        return None
    # Stored credentials cannot retain privileges removed from the account.
    if "admin" in scopes and user.role != "admin":
        return None
    if "agent:write" in scopes and user.role not in {"admin", "operator"}:
        return None
    principal = _principal(
        user,
        "user_api_token",
        token_id=int(candidate.id),
        token_scopes=scopes,
    )
    if principal:
        principal["_token_expires_at"] = candidate.expires_at
    return principal


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Resolve the current user from session cookie, JWT Bearer token, or API key.

    Auth priority:
      0. httpOnly session cookie (browser-based SPA auth)
      1. Authorization: Bearer <JWT> header (API clients, fallback)
      2. X-API-Key header — global key (perimeter auth) or per-user scoped token
    """
    from core.audit_actor import clear_actor, bind_verified_actor
    clear_actor(db)
    # Try 0: httpOnly session cookie (browser-based auth)
    session_token = request.cookies.get("session")
    if session_token:
        token_data = decode_token(session_token)
        if token_data:
            import jwt as _jwt
            try:
                payload = _jwt.decode(
                    session_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM]
                )
                if payload.get("ws"):
                    pass  # ws-tokens are not valid for REST API access — fall through
                elif payload.get("mfa_pending") or payload.get("mfa_setup_required"):
                    pass  # limited-purpose tokens — fall through
                else:
                    jti = payload.get("jti")
                    if jti:
                        blacklisted = db.execute(
                            text("SELECT 1 FROM token_blacklist WHERE jti = :jti"),
                            {"jti": jti},
                        ).fetchone()
                        if blacklisted:
                            pass  # fall through to next auth method
                        else:
                            # R6: only write last_seen_at if it's been >= 5 min
                            # since our last write for this jti. Otherwise SQLite
                            # serializes every authenticated read behind a writer.
                            if _should_write_last_seen(jti):
                                _touch_session_last_seen(db, jti)
                            user = db.execute(
                                text("SELECT * FROM users WHERE username = :username"),
                                {"username": token_data.username},
                            ).fetchone()
                            principal = None if token_predates_account(payload, user) else _principal(user, "session_jwt")
                            if principal:
                                bind_verified_actor(db, principal)
                                return principal
            except Exception:
                log.debug("Cookie auth failed", exc_info=True)

    # Try 1: JWT Bearer token (primary auth)
    if token:
        token_data = decode_token(token)
        if token_data:
            import jwt as _jwt
            payload = None
            try:
                payload = _jwt.decode(
                    token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM]
                )
                # Reject ws-tokens, mfa_pending, and mfa_setup_required tokens from normal routes
                if payload.get("ws") or payload.get("mfa_pending") or payload.get("mfa_setup_required"):
                    return None
                # Check token blacklist (revoked sessions)
                jti = payload.get("jti")
                if jti:
                    blacklisted = db.execute(
                        text("SELECT 1 FROM token_blacklist WHERE jti = :jti"),
                        {"jti": jti},
                    ).fetchone()
                    if blacklisted:
                        return None
                    # R6: batched last_seen_at — write only if cache says it's stale.
                    if _should_write_last_seen(jti):
                        _touch_session_last_seen(db, jti)
            except Exception:
                log.debug("Failed to update session last_seen_at", exc_info=True)
            user = db.execute(
                text("SELECT * FROM users WHERE username = :username"),
                {"username": token_data.username},
            ).fetchone()
            if token_predates_account(payload, user):
                return None
            principal = _principal(user, "bearer_jwt")
            if principal:
                bind_verified_actor(db, principal)
                return principal

    # Try 2: X-API-Key header — check global key first, then scoped user tokens
    api_key = request.headers.get("X-API-Key")
    if api_key and api_key != "undefined":
        # 2a: Global API key (legacy, constant-time comparison)
        configured_key = os.getenv("API_KEY", "")
        if configured_key and hmac.compare_digest(api_key, configured_key):
            admin = db.execute(
                text("SELECT * FROM users WHERE role = 'admin' AND is_active IS TRUE ORDER BY id LIMIT 1")
            ).fetchone()
            principal = _principal(admin, "legacy_global_api_key")
            if principal:
                bind_verified_actor(db, principal)
                mark_noninteractive_principal()
                return principal

        # 2b: Per-user scoped tokens (odin_xxx format)
        principal = resolve_user_api_token(api_key, db)
        from core.api_token_policy import is_camera_only
        if principal and is_camera_only(principal.get("_token_scopes")):
            # Overlay camera tokens authenticate only /overlay/{id}/camera, which
            # resolves them itself; never a general API credential.
            return None
        if principal:
            # Scoped tokens never reach a route without a scope check:
            # routes that declare no scope need the method's generic
            # read/write scope (see core.rbac).
            from core.rbac import enforce_route_token_scope
            enforce_route_token_scope(request, principal)
            bind_verified_actor(db, principal)
            mark_noninteractive_principal()
            return principal

    return None
