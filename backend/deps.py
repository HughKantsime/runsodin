"""
O.D.I.N. — Shared dependencies for API routers.

Extracted from main.py to enable clean router module imports
without circular dependencies.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
import hmac
import time as _time
import os
import json
import logging

from fastapi import Depends, HTTPException, status, Header, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import auth as auth_module
from auth import verify_password, decode_token, has_permission, hash_password, create_access_token
from auth import Token, UserCreate, UserResponse
from models import (
    AuditLog, Base, Printer, Spool, SpoolUsage, SpoolStatus,
    FilamentSlot, Model, Job, JobStatus, FilamentType, FilamentLibrary,
    SchedulerRun, Alert, AlertPreference, AlertType, AlertSeverity,
    PushSubscription, MaintenanceTask, MaintenanceLog, SystemConfig,
    NozzleLifecycle, DryingLog, HYGROSCOPIC_TYPES, PrintPreset, Timelapse,
    VisionDetection, VisionSettings, VisionModel,
    init_db,
)
from config import settings
import crypto

log = logging.getLogger("odin.api")
logger = log  # alias used in some places


# ──────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────

engine = create_engine(
    settings.database_url,
    echo=settings.debug,
    poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
with engine.connect() as conn:
    conn.execute(text("PRAGMA journal_mode=WAL"))
    conn.execute(text("PRAGMA busy_timeout=5000"))

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ──────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """Resolve the current user from JWT or API key."""
    # Try 1: JWT Bearer token (primary auth)
    if token:
        token_data = decode_token(token)
        if token_data:
            from jose import jwt as jose_jwt
            try:
                payload = jose_jwt.decode(
                    token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM]
                )
                # Reject mfa_pending tokens from normal routes
                if payload.get("mfa_pending"):
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


def require_role(required_role: str):
    """FastAPI dependency that checks the user has at least the given role."""
    async def role_checker(current_user: dict = Depends(get_current_user)):
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not has_permission(current_user["role"], required_role):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return role_checker


def _get_org_filter(current_user: dict, request_org_id: int = None) -> Optional[int]:
    """Determine the effective org_id for filtering resources."""
    group_id = current_user.get("group_id") if current_user else None
    role = current_user.get("role", "viewer") if current_user else "viewer"

    if role == "admin" and not group_id:
        return request_org_id  # Superadmin — can optionally filter by org
    if role == "admin" and request_org_id is not None:
        return request_org_id  # Admin overriding their own group scope
    return group_id  # Regular user or org-scoped admin


def get_org_scope(current_user: dict) -> Optional[int]:
    """Return the org_id that should implicitly scope all resource access.

    - Superadmin (role=admin, no group_id): returns None (see everything)
    - Everyone else: returns their group_id (may be None if unassigned)

    For detail endpoints, use ``check_org_access()`` to verify a specific
    resource belongs to the caller's org.
    """
    if not current_user:
        return None
    role = current_user.get("role", "viewer")
    group_id = current_user.get("group_id")
    if role == "admin" and not group_id:
        return None  # superadmin bypass
    return group_id


def check_org_access(current_user: dict, resource_org_id: Optional[int]) -> bool:
    """Check whether the current user may access a resource with the given org_id.

    Rules:
    - Superadmin (admin + no group_id): always True
    - Resource has no org_id (NULL): visible to everyone
    - User's group_id matches resource org_id: True
    - Otherwise: False  (caller should raise 404 to avoid leaking existence)
    """
    if not current_user:
        return False
    role = current_user.get("role", "viewer")
    group_id = current_user.get("group_id")
    # Superadmin sees everything
    if role == "admin" and not group_id:
        return True
    # Unscoped resources are visible to all authenticated users
    if resource_org_id is None:
        return True
    # User must belong to the resource's org
    return group_id is not None and group_id == resource_org_id


# ──────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────

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


def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """Verify API key if authentication is enabled (constant-time comparison)."""
    if not settings.api_key:
        return None
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return x_api_key


def compute_printer_online(printer_dict):
    """Add is_online field based on last_seen within 90 seconds."""
    if printer_dict.get("last_seen"):
        try:
            last = datetime.fromisoformat(str(printer_dict["last_seen"]))
            printer_dict["is_online"] = (datetime.now(timezone.utc) - last).total_seconds() < 90
        except Exception:
            printer_dict["is_online"] = False
    else:
        printer_dict["is_online"] = False
    return printer_dict


def get_printer_api_key(printer: Printer) -> Optional[str]:
    """Get decrypted API key for a printer."""
    if not printer.api_key:
        return None
    return crypto.decrypt(printer.api_key)


def mask_api_key(api_key: Optional[str]) -> Optional[str]:
    """Mask an API key for safe display."""
    if not api_key:
        return None
    if len(api_key) <= 6:
        return "••••••••"
    return "••••••••" + api_key[-6:]


# ──────────────────────────────────────────────
# Password Validation
# ──────────────────────────────────────────────

def _validate_password(password: str) -> tuple:
    """Validate password complexity. Returns (is_valid, message)."""
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"
    if not any(c.islower() for c in password):
        return False, "Password must contain at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    return True, "OK"


# ──────────────────────────────────────────────
# Rate Limiting
# ──────────────────────────────────────────────

_LOGIN_RATE_LIMIT = 10               # max attempts per window
_LOGIN_RATE_WINDOW = 300             # 5 minute window
_LOCKOUT_THRESHOLD = 5               # failed attempts before lockout
_LOCKOUT_DURATION = 900              # 15 minute lockout
_last_login_cleanup = 0              # timestamp of last cleanup


def _login_db_path() -> str:
    return os.environ.get("DATABASE_PATH", "/data/odin.db")


def _check_rate_limit(ip: str) -> bool:
    """Returns True if rate limited.  Queries login_attempts table."""
    try:
        import sqlite3
        conn = sqlite3.connect(_login_db_path(), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        now = _time.time()
        cur = conn.execute(
            "SELECT COUNT(*) FROM login_attempts "
            "WHERE ip = ? AND success = 0 AND attempted_at > ?",
            (ip, now - _LOGIN_RATE_WINDOW),
        )
        count = cur.fetchone()[0]
        conn.close()
        return count >= _LOGIN_RATE_LIMIT
    except Exception:
        return False  # fail open on DB errors


def _record_login_attempt(ip: str, username: str, success: bool, db=None):
    """Record attempt for rate limiting and audit."""
    # Persist to login_attempts table
    try:
        import sqlite3
        conn = sqlite3.connect(_login_db_path(), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(
            "INSERT INTO login_attempts (ip, username, attempted_at, success) "
            "VALUES (?, ?, ?, ?)",
            (ip, username, _time.time(), 1 if success else 0),
        )
        conn.commit()
        conn.close()
    except Exception:
        log.warning("Failed to persist login attempt", exc_info=True)

    # Lazy cleanup every hour
    global _last_login_cleanup
    now = _time.time()
    if now - _last_login_cleanup > 3600:
        _last_login_cleanup = now
        try:
            import sqlite3
            conn = sqlite3.connect(_login_db_path(), timeout=10)
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(
                "DELETE FROM login_attempts WHERE attempted_at < ?",
                (now - _LOCKOUT_DURATION * 2,),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # Log to audit trail if db available
    if db:
        try:
            audit_entry = AuditLog(
                action="login_success" if success else "login_failed",
                entity_type="user",
                details=f"{'Login' if success else 'Failed login'}: {username} from {ip}",
                ip_address=ip,
            )
            db.add(audit_entry)
            db.commit()
        except Exception:
            log.warning("Failed to record login audit entry", exc_info=True)


def _is_locked_out(username: str) -> bool:
    """Returns True if account is locked (>= LOCKOUT_THRESHOLD failures in window)."""
    try:
        import sqlite3
        conn = sqlite3.connect(_login_db_path(), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        now = _time.time()
        cur = conn.execute(
            "SELECT COUNT(*) FROM login_attempts "
            "WHERE username = ? AND success = 0 AND attempted_at > ?",
            (username, now - _LOCKOUT_DURATION),
        )
        count = cur.fetchone()[0]
        conn.close()
        return count >= _LOCKOUT_THRESHOLD
    except Exception:
        return False  # fail open on DB errors
