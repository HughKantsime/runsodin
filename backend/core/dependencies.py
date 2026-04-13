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
from core.models import AuditLog
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
# Fix: cache "last time we updated this jti" in-process and skip the write
# if it's fresh (default: within the last 5 minutes). This means
# active_sessions.last_seen_at is accurate to within 5 minutes, not
# real-time — documented in decision log. Alternative (Redis cache or
# async queue) is a bigger change we'll take later if staleness causes
# problems.
#
# The cache is intentionally process-local: each worker tracks its own
# recent writes. Worst case with multiple workers, the same jti may get
# written up to N times per interval (once per worker). That's still
# orders of magnitude fewer writes than the original "every request"
# behavior.
import threading as _threading

_LAST_SEEN_MIN_INTERVAL_SECONDS = 300  # 5 minutes
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
    if not user:
        return None
    return dict(user._mapping)


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
                                db.execute(
                                    text("UPDATE active_sessions SET last_seen_at = :now WHERE token_jti = :jti"),
                                    {"now": datetime.now(timezone.utc), "jti": jti},
                                )
                                db.commit()
                            user = db.execute(
                                text("SELECT * FROM users WHERE username = :username"),
                                {"username": token_data.username},
                            ).fetchone()
                            if user:
                                return dict(user._mapping)
            except Exception:
                log.debug("Cookie auth failed", exc_info=True)

    # Try 1: JWT Bearer token (primary auth)
    if token:
        token_data = decode_token(token)
        if token_data:
            import jwt as _jwt
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
                        db.execute(
                            text("UPDATE active_sessions SET last_seen_at = :now WHERE token_jti = :jti"),
                            {"now": datetime.now(timezone.utc), "jti": jti},
                        )
                        db.commit()
            except Exception:
                log.debug("Failed to update session last_seen_at", exc_info=True)
            user = db.execute(
                text("SELECT * FROM users WHERE username = :username"),
                {"username": token_data.username},
            ).fetchone()
            if user:
                return dict(user._mapping)

    # Try 2: X-API-Key header — check global key first, then scoped user tokens
    api_key = request.headers.get("X-API-Key")
    if api_key and api_key != "undefined":
        # 2a: Global API key (legacy, constant-time comparison)
        configured_key = os.getenv("API_KEY", "")
        if configured_key and hmac.compare_digest(api_key, configured_key):
            admin = db.execute(
                text("SELECT * FROM users WHERE role = 'admin' AND is_active = 1 ORDER BY id LIMIT 1")
            ).fetchone()
            if admin:
                return dict(admin._mapping)

        # 2b: Per-user scoped tokens (odin_xxx format)
        if api_key.startswith("odin_"):
            prefix = api_key[:10]
            candidates = db.execute(
                text("SELECT * FROM api_tokens WHERE token_prefix = :prefix"),
                {"prefix": prefix},
            ).fetchall()
            for candidate in candidates:
                if verify_password(api_key, candidate.token_hash):
                    # Check expiry
                    if candidate.expires_at:
                        from dateutil.parser import parse as parse_dt
                        try:
                            exp = (
                                parse_dt(candidate.expires_at)
                                if isinstance(candidate.expires_at, str)
                                else candidate.expires_at
                            )
                            if exp < datetime.now(timezone.utc):
                                continue
                        except Exception:
                            pass
                    # Update last_used_at
                    db.execute(
                        text("UPDATE api_tokens SET last_used_at = :now WHERE id = :id"),
                        {"now": datetime.now(timezone.utc), "id": candidate.id},
                    )
                    db.commit()
                    # Fetch the user
                    user = db.execute(
                        text("SELECT * FROM users WHERE id = :id"),
                        {"id": candidate.user_id},
                    ).fetchone()
                    if user:
                        user_dict = dict(user._mapping)
                        user_dict["_token_scopes"] = (
                            json.loads(candidate.scopes) if candidate.scopes else []
                        )
                        return user_dict

    return None


def log_audit(
    db: Session,
    action: str,
    entity_type: str = None,
    entity_id: int = None,
    details: dict = None,
    ip: str = None,
):
    """Stage an audit log entry on the session. Caller MUST commit.

    The entry is attached to ``db`` but not committed here. This is
    intentional: the audit row must be committed in the SAME transaction
    as the business mutation it describes. If callers were to commit the
    business change first and then call this, an audit insert failure
    would leave an un-audited business mutation behind — wrong error
    model, produces duplicate operations on client retry.

    Required call pattern:
        # stage business changes on db
        log_audit(db, "action", ...)
        db.commit()
    """
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        ip_address=ip,
    )
    db.add(entry)
