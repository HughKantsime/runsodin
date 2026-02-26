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

import auth as auth_module
from auth import decode_token, verify_password
from models import AuditLog
from core.db import get_db

log = logging.getLogger("odin.api")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


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
                elif not payload.get("mfa_pending"):
                    jti = payload.get("jti")
                    if jti:
                        blacklisted = db.execute(
                            text("SELECT 1 FROM token_blacklist WHERE jti = :jti"),
                            {"jti": jti},
                        ).fetchone()
                        if blacklisted:
                            pass  # fall through to next auth method
                        else:
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
                # Reject ws-tokens and mfa_pending tokens from normal routes
                if payload.get("ws") or payload.get("mfa_pending"):
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
                    # Update last_seen_at for session tracking
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
    """Log an action to the audit log."""
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        ip_address=ip,
    )
    db.add(entry)
    db.commit()
