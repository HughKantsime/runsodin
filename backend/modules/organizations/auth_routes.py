"""O.D.I.N. — Auth, Users, Sessions, MFA, OIDC, Tokens, Quotas, GDPR, RBAC Routes"""

# Domain: organizations (user management, OIDC, sessions, quotas) + core (login, JWT, MFA)
# Depends on: core
# Owns tables: users, api_tokens, active_sessions, token_blacklist, password_reset_tokens, login_attempts, groups, quota_usage, oidc_config, oidc_pending_states, oidc_auth_codes

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request, Response, UploadFile, File
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import csv, io, json, logging, os, re
from urllib.parse import quote

from core.db import get_db, SessionLocal
from core.dependencies import get_current_user, log_audit, oauth2_scheme
from core.rbac import require_role
from core.auth_helpers import (
    _validate_password, _check_rate_limit, _record_login_attempt, _is_locked_out
)
from core.quota import _get_period_key, _get_quota_usage
import core.auth as auth_module
from core.auth import hash_password, create_access_token, verify_password, decode_token, UserCreate, UserResponse
from core.models import SystemConfig
from core.config import settings
from license_manager import require_feature, check_user_limit
from core.rate_limit import limiter
from core.config import settings as _settings

log = logging.getLogger("odin.api")
router = APIRouter()


# =============================================================================
# Login
# =============================================================================

@router.post("/auth/login", tags=["Auth"])
@limiter.limit("10/minute")
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # Security checks
    client_ip = request.client.host if hasattr(request, 'client') and request.client else "unknown"
    if _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 5 minutes.")
    if _is_locked_out(form_data.username):
        raise HTTPException(status_code=423, detail="Account temporarily locked due to repeated failed attempts. Try again in 15 minutes.")

    user = db.execute(text("SELECT * FROM users WHERE username = :username"),
                      {"username": form_data.username}).fetchone()
    if not user or not verify_password(form_data.password, user.password_hash):
        _record_login_attempt(client_ip, form_data.username, False, db)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled")

    _record_login_attempt(client_ip, form_data.username, True, db)

    db.execute(text("UPDATE users SET last_login = :now WHERE id = :id"),
               {"now": datetime.now(timezone.utc), "id": user.id})
    db.commit()

    # Check MFA
    if user.mfa_enabled:
        mfa_token = create_access_token(
            data={"sub": user.username, "role": user.role, "mfa_pending": True},
            expires_delta=timedelta(minutes=5)
        )
        return {"access_token": mfa_token, "token_type": "bearer", "mfa_required": True}

    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    _record_session(db, user.id, access_token, client_ip, request.headers.get("user-agent", ""))
    log_audit(db, "auth.login", "user", user.id,
              details={"username": user.username},
              ip=client_ip)

    # Set session cookie (httpOnly prevents XSS-based token theft)
    # Also return access_token in body for backward compatibility with API clients
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"access_token": access_token, "token_type": "bearer"})
    resp.set_cookie(
        key="session",
        value=access_token,
        httponly=True,
        secure=_settings.cookie_secure,
        samesite=_settings.cookie_samesite,
        path="/",
        max_age=86400,  # 24 hours, matches JWT expiry
    )
    return resp


# =============================================================================
# Logout
# =============================================================================

@router.post("/auth/logout", tags=["Auth"])
async def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Clear session cookie and blacklist the JWT (if present)."""
    import jwt as _jwt

    def _blacklist_token(token: str) -> None:
        try:
            payload = _jwt.decode(token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                db.execute(
                    text("INSERT OR IGNORE INTO token_blacklist (jti, expires_at) VALUES (:jti, :exp)"),
                    {"jti": jti, "exp": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()},
                )
                db.execute(
                    text("DELETE FROM active_sessions WHERE token_jti = :jti"),
                    {"jti": jti},
                )
        except Exception:
            log.debug("Could not blacklist token on logout", exc_info=True)

    # Blacklist the session cookie JWT
    session_token = request.cookies.get("session")
    if session_token:
        _blacklist_token(session_token)

    # Also blacklist a Bearer JWT (API clients that explicitly call logout)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]
        if bearer_token != session_token:  # avoid double-blacklisting the same token
            _blacklist_token(bearer_token)

    db.commit()

    # Clear the session cookie
    response.delete_cookie(key="session", path="/")
    return {"detail": "Logged out"}


def _record_session(db, user_id, access_token, ip, user_agent):
    """Record an active session from a JWT token."""
    import jwt as _jwt
    try:
        payload = _jwt.decode(access_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
        jti = payload.get("jti")
        if jti:
            db.execute(text("""INSERT OR IGNORE INTO active_sessions (user_id, token_jti, ip_address, user_agent)
                               VALUES (:uid, :jti, :ip, :ua)"""),
                       {"uid": user_id, "jti": jti, "ip": ip, "ua": (user_agent or "")[:500]})
            db.commit()
    except Exception:
        log.warning("Failed to record session", exc_info=True)


# =============================================================================
# MFA / Two-Factor Authentication
# =============================================================================

@router.post("/auth/mfa/verify", tags=["Auth"])
@limiter.limit("10/minute")
async def mfa_verify(request: Request, body: dict, db: Session = Depends(get_db)):
    """Verify TOTP code during login. Requires mfa_pending token."""
    import pyotp

    mfa_token = body.get("mfa_token", "")
    code = body.get("code", "")
    if not mfa_token or not code:
        raise HTTPException(status_code=400, detail="mfa_token and code are required")

    token_data = decode_token(mfa_token)
    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA token")

    # Decode raw payload to check mfa_pending flag
    import jwt as _jwt
    payload = _jwt.decode(mfa_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
    if not payload.get("mfa_pending"):
        raise HTTPException(status_code=400, detail="Token is not an MFA challenge token")

    # Reject blacklisted mfa_pending tokens (already used for a session)
    mfa_pending_jti = payload.get("jti")
    if mfa_pending_jti:
        blacklisted = db.execute(
            text("SELECT 1 FROM token_blacklist WHERE jti = :jti"),
            {"jti": mfa_pending_jti},
        ).fetchone()
        if blacklisted:
            raise HTTPException(status_code=401, detail="MFA token has already been used")

    user = db.execute(text("SELECT * FROM users WHERE username = :username"),
                      {"username": token_data.username}).fetchone()
    if not user or not user.mfa_enabled or not user.mfa_secret:
        raise HTTPException(status_code=400, detail="MFA not configured for this user")

    from core.crypto import decrypt
    secret = decrypt(user.mfa_secret)
    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid MFA code")

    # Blacklist the mfa_pending token so it cannot be reused for a second session
    mfa_jti = payload.get("jti")
    if mfa_jti:
        mfa_exp = payload.get("exp", 0)
        db.execute(text("""
            INSERT OR IGNORE INTO token_blacklist (jti, expires_at)
            VALUES (:jti, :exp)
        """), {"jti": mfa_jti, "exp": datetime.fromtimestamp(mfa_exp, tz=timezone.utc).isoformat()})
        db.commit()

    # Issue full access token
    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    client_ip = request.client.host if hasattr(request, 'client') and request.client else "unknown"
    _record_session(db, user.id, access_token, client_ip, request.headers.get("user-agent", ""))

    from fastapi.responses import JSONResponse
    resp = JSONResponse({"access_token": access_token, "token_type": "bearer"})
    resp.set_cookie(
        key="session",
        value=access_token,
        httponly=True,
        secure=_settings.cookie_secure,
        samesite=_settings.cookie_samesite,
        path="/",
        max_age=86400,
    )
    return resp


@router.post("/auth/mfa/setup", tags=["Auth"])
async def mfa_setup(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Generate TOTP secret and provisioning URI for MFA setup."""
    import pyotp
    import qrcode
    import qrcode.image.svg
    import io, base64

    if current_user.get("mfa_enabled"):
        raise HTTPException(status_code=400, detail="MFA is already enabled. Disable it first.")

    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(
        name=current_user["username"],
        issuer_name="O.D.I.N."
    )

    # Generate QR code as base64 PNG
    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    # Store secret temporarily (encrypted) — not enabled until confirmed
    from core.crypto import encrypt, get_fernet
    if not get_fernet():
        raise HTTPException(
            status_code=503,
            detail="Encryption is not configured on this server. "
            "MFA setup requires ENCRYPTION_KEY to be set."
        )
    encrypted_secret = encrypt(secret)
    db.execute(text("UPDATE users SET mfa_secret = :secret WHERE id = :id"),
               {"secret": encrypted_secret, "id": current_user["id"]})
    db.commit()

    return {
        "secret": secret,
        "provisioning_uri": provisioning_uri,
        "qr_code": f"data:image/png;base64,{qr_b64}",
    }


@router.post("/auth/mfa/confirm", tags=["Auth"])
async def mfa_confirm(body: dict, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Confirm MFA setup by verifying a TOTP code. Enables MFA on the account."""
    import pyotp

    code = body.get("code", "")
    if not code:
        raise HTTPException(status_code=400, detail="TOTP code is required")

    user = db.execute(text("SELECT * FROM users WHERE id = :id"),
                      {"id": current_user["id"]}).fetchone()
    if not user or not user.mfa_secret:
        raise HTTPException(status_code=400, detail="Run /api/auth/mfa/setup first")
    if user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is already enabled")

    from core.crypto import decrypt
    secret = decrypt(user.mfa_secret)
    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code. Scan the QR code and try again.")

    db.execute(text("UPDATE users SET mfa_enabled = 1 WHERE id = :id"),
               {"id": current_user["id"]})
    db.commit()

    log_audit(db, "mfa_enabled", "user", current_user["id"], "MFA enabled")

    return {"status": "ok", "message": "MFA enabled successfully"}


@router.delete("/auth/mfa", tags=["Auth"])
async def mfa_disable(body: dict = None, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Disable MFA. Requires current TOTP code or admin role."""
    import pyotp

    user = db.execute(text("SELECT * FROM users WHERE id = :id"),
                      {"id": current_user["id"]}).fetchone()
    if not user or not user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is not enabled")

    # Require TOTP code to disable (unless admin is disabling for another user)
    code = (body or {}).get("code", "")
    if code:
        from core.crypto import decrypt
        secret = decrypt(user.mfa_secret)
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            raise HTTPException(status_code=401, detail="Invalid MFA code")
    else:
        raise HTTPException(status_code=400, detail="TOTP code is required to disable MFA")

    db.execute(text("UPDATE users SET mfa_enabled = 0, mfa_secret = NULL WHERE id = :id"),
               {"id": current_user["id"]})
    db.commit()

    log_audit(db, "mfa_disabled", "user", current_user["id"], "MFA disabled")

    return {"status": "ok", "message": "MFA disabled"}


@router.delete("/admin/users/{user_id}/mfa", tags=["Auth"])
async def admin_mfa_disable(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: force-disable MFA for a user (no TOTP required)."""
    user = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is not enabled for this user")

    db.execute(text("UPDATE users SET mfa_enabled = 0, mfa_secret = NULL WHERE id = :id"),
               {"id": user_id})
    db.commit()

    log_audit(db, "mfa_disabled_admin", "user", user_id,
             f"Admin force-disabled MFA for user {user.username}")

    return {"status": "ok", "message": f"MFA disabled for {user.username}"}


@router.get("/auth/mfa/status", tags=["Auth"])
async def mfa_status(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get MFA status for the current user."""
    return {"mfa_enabled": bool(current_user.get("mfa_enabled"))}


@router.get("/config/require-mfa", tags=["Config"])
async def get_require_mfa(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get whether MFA is required for all users."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'require_mfa'")).fetchone()
    return {"require_mfa": row[0] == "true" if row else False}


@router.put("/config/require-mfa", tags=["Config"])
async def set_require_mfa(body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set whether MFA is required for all users."""
    require = bool(body.get("require_mfa", False))
    db.execute(text("""INSERT INTO system_config (key, value) VALUES ('require_mfa', :val)
                       ON CONFLICT(key) DO UPDATE SET value = :val"""),
               {"val": "true" if require else "false"})
    db.commit()
    return {"require_mfa": require}


# =============================================================================
# Session Management
# =============================================================================

@router.get("/sessions", tags=["Sessions"])
async def list_sessions(request: Request, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """List active sessions for the current user."""
    rows = db.execute(text(
        "SELECT s.id, s.token_jti, s.ip_address, s.user_agent, s.created_at, s.last_seen_at "
        "FROM active_sessions s WHERE s.user_id = :uid ORDER BY s.last_seen_at DESC"),
        {"uid": current_user["id"]}).fetchall()

    # Determine current session's jti — check cookie first (browser), then Bearer header (API clients)
    current_jti = None
    session_cookie = request.cookies.get("session")
    auth_header = request.headers.get("authorization", "")
    raw_token = session_cookie or (auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else None)
    if raw_token:
        try:
            import jwt as _jwt
            payload = _jwt.decode(raw_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
            current_jti = payload.get("jti")
        except Exception:
            pass

    return [{
        "id": r.id,
        "ip_address": r.ip_address,
        "user_agent": r.user_agent,
        "created_at": r.created_at,
        "last_seen_at": r.last_seen_at,
        "is_current": r.token_jti == current_jti,
    } for r in rows]


@router.delete("/sessions/{session_id}", tags=["Sessions"])
async def revoke_session(session_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Revoke a specific session."""
    row = db.execute(text("SELECT * FROM active_sessions WHERE id = :id AND user_id = :uid"),
                     {"id": session_id, "uid": current_user["id"]}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    # Add to blacklist (expires when the JWT would expire — 24h from creation)
    from dateutil.parser import parse as parse_dt
    try:
        created = parse_dt(row.created_at) if isinstance(row.created_at, str) else row.created_at
        expires_at = created + timedelta(hours=24)
    except Exception:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    db.execute(text("INSERT OR IGNORE INTO token_blacklist (jti, expires_at) VALUES (:jti, :exp)"),
               {"jti": row.token_jti, "exp": expires_at})
    db.execute(text("DELETE FROM active_sessions WHERE id = :id"), {"id": session_id})
    db.commit()

    return {"status": "ok"}


@router.delete("/sessions", tags=["Sessions"])
async def revoke_all_sessions(request: Request, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Revoke all sessions except the current one."""
    # Find current jti — check cookie first (browser), then Bearer header (API clients)
    current_jti = None
    session_cookie = request.cookies.get("session")
    auth_header = request.headers.get("authorization", "")
    raw_token = session_cookie or (auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else None)
    if raw_token:
        try:
            import jwt as _jwt
            payload = _jwt.decode(raw_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
            current_jti = payload.get("jti")
        except Exception:
            pass

    rows = db.execute(text("SELECT token_jti, created_at FROM active_sessions WHERE user_id = :uid"),
                      {"uid": current_user["id"]}).fetchall()
    count = 0
    for r in rows:
        if r.token_jti == current_jti:
            continue
        try:
            from dateutil.parser import parse as parse_dt
            created = parse_dt(r.created_at) if isinstance(r.created_at, str) else r.created_at
            expires_at = created + timedelta(hours=24)
        except Exception:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        db.execute(text("INSERT OR IGNORE INTO token_blacklist (jti, expires_at) VALUES (:jti, :exp)"),
                   {"jti": r.token_jti, "exp": expires_at})
        count += 1

    db.execute(text("DELETE FROM active_sessions WHERE user_id = :uid AND token_jti != :jti"),
               {"uid": current_user["id"], "jti": current_jti or ""})
    db.commit()

    return {"status": "ok", "revoked": count}


@router.get("/admin/sessions", tags=["Sessions"])
async def admin_list_sessions(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: list all active sessions across all users."""
    rows = db.execute(text(
        "SELECT s.id, s.user_id, u.username, s.ip_address, s.user_agent, s.created_at, s.last_seen_at "
        "FROM active_sessions s JOIN users u ON s.user_id = u.id "
        "ORDER BY s.last_seen_at DESC LIMIT 200")).fetchall()
    return [{
        "id": r.id, "user_id": r.user_id, "username": r.username,
        "ip_address": r.ip_address, "user_agent": r.user_agent,
        "created_at": r.created_at, "last_seen_at": r.last_seen_at,
    } for r in rows]


@router.delete("/admin/sessions/{session_id}", tags=["Sessions"])
async def admin_revoke_session(session_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: force-revoke any session."""
    row = db.execute(text("SELECT * FROM active_sessions WHERE id = :id"), {"id": session_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from dateutil.parser import parse as parse_dt
        created = parse_dt(row.created_at) if isinstance(row.created_at, str) else row.created_at
        expires_at = created + timedelta(hours=24)
    except Exception:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    db.execute(text("INSERT OR IGNORE INTO token_blacklist (jti, expires_at) VALUES (:jti, :exp)"),
               {"jti": row.token_jti, "exp": expires_at})
    db.execute(text("DELETE FROM active_sessions WHERE id = :id"), {"id": session_id})
    db.commit()

    log_audit(db, "session_revoked_admin", "session", session_id,
              f"Admin revoked session for user_id={row.user_id}")

    return {"status": "ok"}


# =============================================================================
# Scoped API Tokens (Per-User)
# =============================================================================

VALID_SCOPES = {
    # Umbrella scopes — grant read or write access across all resources
    "read", "write",
    # Granular resource scopes
    "read:printers", "write:printers",
    "read:jobs", "write:jobs",
    "read:spools", "write:spools",
    "read:models", "write:models",
    "read:analytics",
    "admin",
}


@router.post("/tokens", tags=["API Tokens"])
async def create_api_token(body: dict, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Create a new scoped API token for the current user."""
    import secrets
    name = body.get("name", "").strip()
    scopes = body.get("scopes", [])
    expires_days = body.get("expires_days")

    if not name:
        raise HTTPException(status_code=400, detail="Token name is required")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="Token name too long")

    # Validate scopes
    if not isinstance(scopes, list):
        raise HTTPException(status_code=400, detail="Scopes must be a list")
    invalid = set(scopes) - VALID_SCOPES
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid scopes: {', '.join(invalid)}")

    # Non-admins can't grant admin scope
    if "admin" in scopes and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create tokens with admin scope")

    # Generate token
    raw_token = f"odin_{secrets.token_urlsafe(32)}"
    token_prefix = raw_token[:10]
    token_hash_val = hash_password(raw_token)

    expires_at = None
    if expires_days and int(expires_days) > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(expires_days))

    db.execute(text("""INSERT INTO api_tokens (user_id, name, token_hash, token_prefix, scopes, expires_at)
                       VALUES (:user_id, :name, :token_hash, :prefix, :scopes, :expires_at)"""),
               {"user_id": current_user["id"], "name": name, "token_hash": token_hash_val,
                "prefix": token_prefix, "scopes": json.dumps(scopes), "expires_at": expires_at})
    db.commit()

    token_id = db.execute(text("SELECT last_insert_rowid()")).scalar()

    log_audit(db, "api_token_created", "api_token", token_id, f"Token '{name}' created")

    return {
        "id": token_id,
        "name": name,
        "token": raw_token,  # Only returned once
        "prefix": token_prefix,
        "scopes": scopes,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/tokens", tags=["API Tokens"])
async def list_api_tokens(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """List all API tokens for the current user."""
    rows = db.execute(text(
        "SELECT id, name, token_prefix, scopes, expires_at, last_used_at, created_at "
        "FROM api_tokens WHERE user_id = :uid ORDER BY created_at DESC"),
        {"uid": current_user["id"]}).fetchall()
    return [{
        "id": r.id, "name": r.name, "prefix": r.token_prefix,
        "scopes": json.loads(r.scopes) if r.scopes else [],
        "expires_at": r.expires_at, "last_used_at": r.last_used_at,
        "created_at": r.created_at,
    } for r in rows]


@router.delete("/tokens/{token_id}", tags=["API Tokens"])
async def revoke_api_token(token_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Revoke (delete) an API token."""
    row = db.execute(text("SELECT * FROM api_tokens WHERE id = :id AND user_id = :uid"),
                     {"id": token_id, "uid": current_user["id"]}).fetchone()
    if not row:
        # Admins can delete any token
        if current_user["role"] == "admin":
            row = db.execute(text("SELECT * FROM api_tokens WHERE id = :id"), {"id": token_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Token not found")

    db.execute(text("DELETE FROM api_tokens WHERE id = :id"), {"id": token_id})
    db.commit()

    log_audit(db, "api_token_revoked", "api_token", token_id, f"Token '{row.name}' revoked")

    return {"status": "ok"}


# =============================================================================
# Print Quotas
# =============================================================================

@router.get("/quotas", tags=["Quotas"])
async def get_my_quota(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get current user's quota config and usage."""
    period = current_user.get("quota_period") or "monthly"
    usage = _get_quota_usage(db, current_user["id"], period)
    return {
        "quota_grams": current_user.get("quota_grams"),
        "quota_hours": current_user.get("quota_hours"),
        "quota_jobs": current_user.get("quota_jobs"),
        "quota_period": period,
        "usage": {
            "grams_used": usage["grams_used"],
            "hours_used": usage["hours_used"],
            "jobs_used": usage["jobs_used"],
        },
        "period_key": usage["period_key"],
    }


@router.get("/admin/quotas", tags=["Quotas"])
async def admin_list_quotas(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: list all users' quota config and usage."""
    users = db.execute(text(
        "SELECT id, username, quota_grams, quota_hours, quota_jobs, quota_period FROM users WHERE is_active = 1"
    )).fetchall()
    result = []
    for u in users:
        period = u.quota_period or "monthly"
        usage = _get_quota_usage(db, u.id, period)
        result.append({
            "user_id": u.id, "username": u.username,
            "quota_grams": u.quota_grams, "quota_hours": u.quota_hours,
            "quota_jobs": u.quota_jobs, "quota_period": period,
            "usage": {"grams_used": usage["grams_used"], "hours_used": usage["hours_used"], "jobs_used": usage["jobs_used"]},
        })
    return result


@router.put("/admin/quotas/{user_id}", tags=["Quotas"])
async def admin_set_quota(user_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: set quotas for a user."""
    user = db.execute(text("SELECT id FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    sets = []
    params = {"id": user_id}
    for field in ["quota_grams", "quota_hours", "quota_jobs", "quota_period"]:
        if field in body:
            sets.append(f"{field} = :{field}")
            params[field] = body[field]

    if sets:
        db.execute(text(f"UPDATE users SET {', '.join(sets)} WHERE id = :id"), params)
        db.commit()

    log_audit(db, "quota_updated", "user", user_id, f"Quotas updated: {body}")
    return {"status": "ok"}


# =============================================================================
# GDPR Data Export & Erasure
# =============================================================================

@router.get("/users/{user_id}/export", tags=["GDPR"])
async def export_user_data(user_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Export all personal data for a user (GDPR Article 20)."""
    # Users can export their own data; admins can export anyone's
    if current_user["id"] != user_id and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Can only export your own data")

    user = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    u = dict(user._mapping)
    # Strip sensitive fields
    u.pop("password_hash", None)
    u.pop("mfa_secret", None)

    # Collect related data
    jobs = [dict(r._mapping) for r in db.execute(
        text("SELECT * FROM jobs WHERE submitted_by = :uid"), {"uid": user_id}).fetchall()]
    audit = [dict(r._mapping) for r in db.execute(
        text("SELECT * FROM audit_logs WHERE entity_type = 'user' AND entity_id = :uid ORDER BY timestamp DESC LIMIT 1000"),
        {"uid": user_id}).fetchall()]
    sessions_data = [dict(r._mapping) for r in db.execute(
        text("SELECT id, ip_address, user_agent, created_at, last_seen_at FROM active_sessions WHERE user_id = :uid"),
        {"uid": user_id}).fetchall()]
    prefs = [dict(r._mapping) for r in db.execute(
        text("SELECT * FROM alert_preferences WHERE user_id = :uid"), {"uid": user_id}).fetchall()]

    # API tokens: exclude token_hash and token_prefix to avoid leaking token material
    api_tokens_data = [dict(r._mapping) for r in db.execute(
        text("SELECT id, name, scopes, created_at, last_used_at, expires_at FROM api_tokens WHERE user_id = :uid"),
        {"uid": user_id}).fetchall()]

    # Quota usage records
    quota_data = [dict(r._mapping) for r in db.execute(
        text("SELECT period_key, grams_used, hours_used, jobs_used, updated_at FROM quota_usage WHERE user_id = :uid"),
        {"uid": user_id}).fetchall()]

    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": u,
        "jobs_submitted": jobs,
        "audit_log_entries": audit,
        "active_sessions": sessions_data,
        "alert_preferences": prefs,
        "api_tokens": api_tokens_data,
        "quota_usage": quota_data,
    }

    log_audit(db, "gdpr_export", "user", user_id, f"Data exported for user {user.username}")
    return export


@router.delete("/users/{user_id}/erase", tags=["GDPR"])
async def erase_user_data(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Anonymize user data (GDPR Article 17). Admin only. Preserves job records for analytics."""
    user = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        admin_count = db.execute(text("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1")).scalar()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot erase the last admin account")

    # Anonymize user record
    db.execute(text("""UPDATE users SET
        username = :anon_name, email = '[deleted]', password_hash = '[deleted]',
        is_active = 0, mfa_enabled = 0, mfa_secret = NULL,
        oidc_subject = NULL, oidc_provider = NULL
        WHERE id = :id"""),
        {"anon_name": f"[deleted-{user_id}]", "id": user_id})

    # Clean up related data
    db.execute(text("DELETE FROM active_sessions WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM api_tokens WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM alert_preferences WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM push_subscriptions WHERE user_id = :uid"), {"uid": user_id})
    db.commit()

    log_audit(db, "gdpr_erasure", "user", user_id, f"User data erased (was: {user.username})")
    return {"status": "ok", "message": f"User {user.username} data erased"}


# =============================================================================
# Data Retention Policies
# =============================================================================

RETENTION_DEFAULTS = {
    "completed_jobs_days": 0,       # 0 = unlimited
    "audit_logs_days": 365,
    "timelapses_days": 30,
    "alert_history_days": 90,
}


@router.get("/config/retention", tags=["Config"])
async def get_retention_config(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get data retention policy configuration."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'data_retention'")).fetchone()
    if not row:
        return RETENTION_DEFAULTS
    val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
    return {**RETENTION_DEFAULTS, **val}


@router.put("/config/retention", tags=["Config"])
async def set_retention_config(body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set data retention policy configuration."""
    config = {}
    for key in RETENTION_DEFAULTS:
        if key in body:
            val = int(body[key])
            if val < 0:
                raise HTTPException(status_code=400, detail=f"{key} must be >= 0")
            config[key] = val

    db.execute(text("""INSERT INTO system_config (key, value) VALUES ('data_retention', :val)
                       ON CONFLICT(key) DO UPDATE SET value = :val"""),
               {"val": json.dumps(config)})
    db.commit()

    log_audit(db, "retention_updated", details=f"Retention config: {config}")
    return {**RETENTION_DEFAULTS, **config}


@router.post("/admin/retention/cleanup", tags=["Config"])
async def run_retention_cleanup(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Manually trigger data retention cleanup."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'data_retention'")).fetchone()
    config = {**RETENTION_DEFAULTS}
    if row:
        val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
        config.update(val)

    deleted = {}
    now = datetime.now(timezone.utc)

    if config["completed_jobs_days"] > 0:
        cutoff = now - timedelta(days=config["completed_jobs_days"])
        r = db.execute(text("DELETE FROM jobs WHERE status IN ('completed','failed','cancelled') AND updated_at < :cutoff"),
                       {"cutoff": cutoff})
        deleted["completed_jobs"] = r.rowcount

    if config["audit_logs_days"] > 0:
        cutoff = now - timedelta(days=config["audit_logs_days"])
        r = db.execute(text("DELETE FROM audit_logs WHERE timestamp < :cutoff"), {"cutoff": cutoff})
        deleted["audit_logs"] = r.rowcount

    if config["alert_history_days"] > 0:
        cutoff = now - timedelta(days=config["alert_history_days"])
        r = db.execute(text("DELETE FROM alerts WHERE created_at < :cutoff"), {"cutoff": cutoff})
        deleted["alerts"] = r.rowcount

    if config["timelapses_days"] > 0:
        cutoff = now - timedelta(days=config["timelapses_days"])
        r = db.execute(text("DELETE FROM timelapses WHERE created_at < :cutoff"), {"cutoff": cutoff})
        deleted["timelapses"] = r.rowcount

    # Clean expired token blacklist entries
    db.execute(text("DELETE FROM token_blacklist WHERE expires_at < :now"), {"now": now})
    # Clean stale sessions (older than 24h with no JWT to match)
    stale = now - timedelta(hours=48)
    db.execute(text("DELETE FROM active_sessions WHERE last_seen_at < :cutoff"), {"cutoff": stale})

    db.commit()
    return {"status": "ok", "deleted": deleted}


# =============================================================================
# OIDC / SSO Authentication
# =============================================================================

@router.get("/auth/oidc/config", tags=["Auth"])
async def get_oidc_public_config(db: Session = Depends(get_db)):
    """Get public OIDC config for login page (is SSO enabled, display name)."""
    row = db.execute(text("SELECT is_enabled, display_name FROM oidc_config LIMIT 1")).fetchone()
    if not row:
        return {"enabled": False}
    return {
        "enabled": bool(row[0]),
        "display_name": row[1] or "Single Sign-On",
    }


@router.get("/auth/oidc/login", tags=["Auth"])
async def oidc_login(request: Request, db: Session = Depends(get_db)):
    """Initiate OIDC login flow. Redirects to identity provider."""
    from modules.organizations.oidc_handler import create_handler_from_config

    row = db.execute(text("SELECT * FROM oidc_config WHERE is_enabled = 1 LIMIT 1")).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="OIDC not configured")

    config = dict(row._mapping)

    # Build redirect URI — use configured override when behind a reverse proxy
    if _settings.oidc_redirect_uri:
        redirect_uri = _settings.oidc_redirect_uri
    else:
        base_url = str(request.base_url).rstrip("/")
        redirect_uri = f"{base_url}/api/auth/oidc/callback"

    handler = create_handler_from_config(config, redirect_uri)

    url, state = await handler.get_authorization_url()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=url, status_code=302)


@router.get("/auth/oidc/callback", tags=["Auth"])
async def oidc_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    error_description: str = None,
    db: Session = Depends(get_db)
):
    """Handle OIDC callback from identity provider."""
    from modules.organizations.oidc_handler import create_handler_from_config
    from fastapi.responses import RedirectResponse

    # Handle errors from provider
    if error:
        log.error(f"OIDC error: {error} - {error_description}")
        return RedirectResponse(url=f"/?error={quote(str(error))}", status_code=302)

    if not code or not state:
        return RedirectResponse(url="/?error=missing_params", status_code=302)

    # Get OIDC config
    row = db.execute(text("SELECT * FROM oidc_config WHERE is_enabled = 1 LIMIT 1")).fetchone()
    if not row:
        return RedirectResponse(url="/?error=oidc_not_configured", status_code=302)

    config = dict(row._mapping)

    if _settings.oidc_redirect_uri:
        redirect_uri = _settings.oidc_redirect_uri
    else:
        base_url = str(request.base_url).rstrip("/")
        redirect_uri = f"{base_url}/api/auth/oidc/callback"

    handler = create_handler_from_config(config, redirect_uri)

    # Validate state
    if not handler.validate_state(state):
        return RedirectResponse(url="/?error=invalid_state", status_code=302)

    try:
        # Exchange code for tokens
        tokens = await handler.exchange_code(code)

        # Parse and validate ID token signature
        id_token_claims = await handler.parse_id_token(tokens["id_token"])

        # Also get user info for more details
        user_info = await handler.get_user_info(tokens["access_token"])

        # Extract user details
        oidc_subject = id_token_claims.get("sub") or id_token_claims.get("oid")
        email = user_info.get("mail") or user_info.get("userPrincipalName") or id_token_claims.get("email")
        display_name = user_info.get("displayName") or id_token_claims.get("name") or email

        if not oidc_subject or not email:
            log.error(f"Missing required claims: sub={oidc_subject}, email={email}")
            return RedirectResponse(url="/?error=missing_claims", status_code=302)

        # Find or create user
        oidc_provider = config.get("display_name", "oidc").lower().replace(" ", "_")
        existing = db.execute(
            text("SELECT * FROM users WHERE oidc_subject = :sub AND oidc_provider = :provider"),
            {"sub": oidc_subject, "provider": oidc_provider}
        ).fetchone()

        if existing:
            # Update last login
            user_id = existing[0]
            db.execute(
                text("UPDATE users SET last_login = :now, email = :email WHERE id = :id"),
                {"now": datetime.now(timezone.utc).isoformat(), "email": email, "id": user_id}
            )
            db.commit()
            user_role = existing._mapping.get("role", "operator")
        elif config.get("auto_create_users", False):
            # Create new user
            username = email.split("@")[0]  # Use email prefix as username
            default_role = config.get("default_role", "viewer")

            # Ensure unique username
            base_username = username
            counter = 1
            while db.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username}).fetchone():
                username = f"{base_username}{counter}"
                counter += 1

            db.execute(
                text("""
                    INSERT INTO users (username, email, password_hash, role, oidc_subject, oidc_provider, last_login)
                    VALUES (:username, :email, '', :role, :sub, :provider, :now)
                """),
                {
                    "username": username,
                    "email": email,
                    "role": default_role,
                    "sub": oidc_subject,
                    "provider": oidc_provider,
                    "now": datetime.now(timezone.utc).isoformat(),
                }
            )
            db.commit()

            user_id = db.execute(text("SELECT last_insert_rowid()")).fetchone()[0]
            user_role = default_role
            log.info(f"Created OIDC user: {username} ({email})")
        else:
            log.warning(f"OIDC user not found and auto-create disabled: {email}")
            return RedirectResponse(url="/?error=user_not_found", status_code=302)

        # Generate JWT — use the same secret/function as normal login
        access_token = create_access_token(
            data={
                "sub": existing._mapping.get("username") if existing else username,
                "role": user_role,
            }
        )

        # Store a short-lived one-time code that the frontend can exchange for the JWT.
        # This avoids leaking the JWT in the redirect URL (browser history, Referer header, logs).
        # The token itself is Fernet-encrypted so a SQLite dump doesn't yield usable JWTs.
        import secrets as _secrets
        from core.crypto import encrypt as _crypto_encrypt
        oidc_code = _secrets.token_urlsafe(48)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        db.execute(text(
            "INSERT INTO oidc_auth_codes (code, access_token, expires_at) VALUES (:code, :token, :exp)"
        ), {"code": oidc_code, "token": _crypto_encrypt(access_token), "exp": expires_at})
        db.commit()

        return RedirectResponse(
            url=f"/?oidc_code={oidc_code}",
            status_code=302
        )

    except Exception as e:
        log.error(f"OIDC callback error: {e}", exc_info=True)
        return RedirectResponse(url=f"/?error=auth_failed", status_code=302)


@router.post("/auth/oidc/exchange", tags=["Auth"])
async def oidc_exchange_code(body: dict, request: Request, db: Session = Depends(get_db)):
    """Exchange a one-time OIDC auth code for a JWT access token.

    The OIDC callback redirects the browser with a short-lived code instead of
    the JWT itself, so the token never appears in browser history or server logs.
    """
    code = body.get("code", "")
    if not code:
        raise HTTPException(status_code=400, detail="code is required")

    row = db.execute(
        text("SELECT access_token, expires_at FROM oidc_auth_codes WHERE code = :code"),
        {"code": code},
    ).fetchone()

    # Always delete the code (one-time use)
    db.execute(text("DELETE FROM oidc_auth_codes WHERE code = :code"), {"code": code})
    # Also clean up expired codes
    db.execute(
        text("DELETE FROM oidc_auth_codes WHERE expires_at < :now"),
        {"now": datetime.now(timezone.utc).isoformat()},
    )
    db.commit()

    if not row:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    expires_at = row.expires_at
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=400, detail="Code expired")

    # Decrypt the stored token (encrypted at storage time to protect against DB dump)
    from core.crypto import decrypt as _crypto_decrypt, is_encrypted as _crypto_is_encrypted
    raw_token = row.access_token
    access_token = _crypto_decrypt(raw_token) if _crypto_is_encrypted(raw_token) else raw_token

    # Record the session and set a proper httpOnly cookie (same as password login)
    import jwt as _jwt
    try:
        payload = _jwt.decode(access_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
        username = payload.get("sub")
        if username:
            user = db.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username}).fetchone()
            if user:
                client_ip = request.client.host if request.client else "unknown"
                _record_session(db, user.id, access_token, client_ip, request.headers.get("user-agent", ""))
    except Exception:
        log.debug("Could not record OIDC session", exc_info=True)

    from fastapi.responses import JSONResponse
    resp = JSONResponse({"access_token": access_token, "token_type": "bearer"})
    resp.set_cookie(
        key="session",
        value=access_token,
        httponly=True,
        secure=_settings.cookie_secure,
        samesite=_settings.cookie_samesite,
        path="/",
        max_age=86400,
    )
    return resp


@router.get("/admin/oidc", tags=["Admin"])
async def get_oidc_config(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Get full OIDC configuration (admin only)."""
    row = db.execute(text("SELECT * FROM oidc_config LIMIT 1")).fetchone()
    if not row:
        return {"configured": False}

    config = dict(row._mapping)
    # Don't return the encrypted secret
    if "client_secret_encrypted" in config:
        config["has_client_secret"] = bool(config["client_secret_encrypted"])
        del config["client_secret_encrypted"]

    return config


@router.put("/admin/oidc", tags=["Admin"])
async def update_oidc_config(
    request: Request,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Update OIDC configuration (admin only)."""
    data = await request.json()

    # Encrypt client secret if provided
    client_secret = data.get("client_secret")
    if client_secret:
        from core.crypto import encrypt
        data["client_secret_encrypted"] = encrypt(client_secret)
        del data["client_secret"]

    # Build update query
    allowed_fields = [
        "display_name", "client_id", "client_secret_encrypted", "tenant_id",
        "discovery_url", "scopes", "auto_create_users", "default_role", "is_enabled"
    ]

    updates = []
    params = {}
    for field in allowed_fields:
        if field in data:
            updates.append(f"{field} = :{field}")
            params[field] = data[field]

    if updates:
        updates.append("updated_at = datetime('now')")
        query = f"UPDATE oidc_config SET {', '.join(updates)} WHERE id = 1"
        db.execute(text(query), params)
        db.commit()

    return {"success": True}


# =============================================================================
# Auth: Get Me
# =============================================================================

@router.get("/auth/me", tags=["Auth"])
async def get_me(current_user: dict = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": current_user["username"], "email": current_user["email"], "role": current_user["role"], "group_id": current_user.get("group_id")}


@router.get("/auth/me/theme", tags=["Auth"])
async def get_theme(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get the current user's theme preferences."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    row = db.execute(
        text("SELECT theme_json FROM users WHERE id = :id"),
        {"id": current_user["id"]},
    ).fetchone()
    if row and row[0]:
        import json as _json
        try:
            return _json.loads(row[0])
        except Exception:
            pass
    return {"accent_color": "#6366f1", "sidebar_style": "dark", "background": "default"}


@router.put("/auth/me/theme", tags=["Auth"])
async def set_theme(request: Request, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Set the current user's theme preferences."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    import json as _json
    body = await request.json()
    theme_json = _json.dumps(body)
    db.execute(
        text("UPDATE users SET theme_json = :t WHERE id = :id"),
        {"t": theme_json, "id": current_user["id"]},
    )
    db.commit()
    return body


@router.post("/auth/ws-token", tags=["Auth"])
async def get_ws_token(current_user: dict = Depends(get_current_user)):
    """Issue a short-lived JWT for WebSocket authentication.

    WebSocket connections cannot send cookies or custom headers, so they need
    a token passed as a query parameter (?token=...). This endpoint issues a
    5-minute JWT specifically for that purpose, callable from the authenticated
    browser session (cookie auth).
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ws_token = create_access_token(
        data={"sub": current_user["username"], "role": current_user["role"], "ws": True},
        expires_delta=timedelta(minutes=5),
    )
    return {"token": ws_token}


# =============================================================================
# Users CRUD
# =============================================================================

@router.get("/users", tags=["Users"])
async def list_users(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    users = db.execute(text("SELECT id, username, email, role, is_active, last_login, created_at, group_id FROM users")).fetchall()
    return [dict(u._mapping) for u in users]

def _send_odin_email(db, to_email: str, subject: str, html_body: str):
    """Send an email using the configured SMTP settings. Returns True on success."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    if not config:
        return False
    smtp = config.value
    if not smtp.get("enabled") or not smtp.get("host"):
        return False

    password = smtp.get("password", "")
    if password:
        try:
            import core.crypto as crypto
            password = crypto.decrypt(password)
        except Exception:
            pass

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp.get("from_address", smtp.get("username", "odin@localhost"))
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        if smtp.get("use_tls", True):
            server = smtplib.SMTP(smtp["host"], smtp.get("port", 587), timeout=10)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp["host"], smtp.get("port", 25), timeout=10)
        if smtp.get("username") and password:
            server.login(smtp["username"], password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        log.error(f"SMTP send failed: {e}")
        return False


@router.post("/users", tags=["Users"])
async def create_user(user: UserCreate, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    # Check license user limit
    current_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
    check_user_limit(current_count)

    import secrets as _secrets
    password = user.password
    if user.send_welcome_email and user.email:
        # Auto-generate a secure random password for welcome email flow
        password = _secrets.token_urlsafe(16)

    if not password:
        raise HTTPException(status_code=400, detail="Password is required")

    password_hash = hash_password(password)
    try:
        db.execute(text("""
            INSERT INTO users (username, email, password_hash, role, group_id)
            VALUES (:username, :email, :password_hash, :role, :group_id)
        """), {"username": user.username, "email": user.email, "password_hash": password_hash, "role": user.role, "group_id": user.group_id})
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Username or email already exists")

    if user.send_welcome_email and user.email:
        html = f"""
        <html><body style="font-family: Arial, sans-serif; padding: 20px; background: #1a1a1a; color: #e0e0e0;">
        <div style="max-width: 500px; margin: 0 auto; background: #2a2a2a; padding: 20px; border-radius: 8px;">
            <h2 style="color: #3b82f6; margin-top: 0;">Welcome to O.D.I.N.</h2>
            <p>Your account has been created. Here are your login details:</p>
            <p><strong>Username:</strong> {user.username}<br>
               <strong>Temporary Password:</strong> {password}</p>
            <p>Please change your password after your first login.</p>
            <hr style="border: none; border-top: 1px solid #444; margin: 20px 0;">
            <p style="color: #888; font-size: 12px;">O.D.I.N. — Orchestrated Dispatch &amp; Inventory Network</p>
        </div></body></html>
        """
        _send_odin_email(db, user.email, "Your O.D.I.N. Account", html)

    return {"status": "created"}


@router.post("/users/{user_id}/reset-password-email", tags=["Users"])
async def reset_password_email(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin action: generate a new random password and email it to the user."""
    import secrets as _secrets

    user_row = db.execute(text("SELECT username, email FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    if not user_row[1]:
        raise HTTPException(status_code=400, detail="User has no email address")

    new_password = _secrets.token_urlsafe(16)
    password_hash = hash_password(new_password)
    db.execute(text("UPDATE users SET password_hash = :h WHERE id = :id"), {"h": password_hash, "id": user_id})
    db.commit()

    html = f"""
    <html><body style="font-family: Arial, sans-serif; padding: 20px; background: #1a1a1a; color: #e0e0e0;">
    <div style="max-width: 500px; margin: 0 auto; background: #2a2a2a; padding: 20px; border-radius: 8px;">
        <h2 style="color: #3b82f6; margin-top: 0;">Password Reset — O.D.I.N.</h2>
        <p>Your password has been reset by an administrator.</p>
        <p><strong>Username:</strong> {user_row[0]}<br>
           <strong>New Password:</strong> {new_password}</p>
        <p>Please change your password after logging in.</p>
        <hr style="border: none; border-top: 1px solid #444; margin: 20px 0;">
        <p style="color: #888; font-size: 12px;">O.D.I.N.</p>
    </div></body></html>
    """
    sent = _send_odin_email(db, user_row[1], "O.D.I.N. Password Reset", html)
    if not sent:
        raise HTTPException(status_code=503, detail="SMTP is not configured or send failed")

    log_audit(db, "user.password_reset_email", "user", user_id,
              {"actor_user_id": current_user["id"], "target_user_id": user_id})
    return {"status": "password_reset_email_sent"}

@router.patch("/users/{user_id}", tags=["Users"])
async def update_user(user_id: int, updates: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if 'password' in updates and updates['password']:
        pw_valid, pw_msg = _validate_password(updates['password'])
        if not pw_valid:
            raise HTTPException(status_code=400, detail=pw_msg)
        updates['password_hash'] = hash_password(updates.pop('password'))
    else:
        updates.pop('password', None)

    # SB-6: Whitelist allowed columns to prevent SQL injection via column names
    ALLOWED_USER_FIELDS = {"username", "email", "role", "is_active", "password_hash", "group_id"}
    updates = {k: v for k, v in updates.items() if k in ALLOWED_USER_FIELDS}

    password_changed = 'password_hash' in updates
    if updates:
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
        updates['id'] = user_id
        db.execute(text(f"UPDATE users SET {set_clause} WHERE id = :id"), updates)
        db.commit()
    if password_changed:
        log_audit(db, "user.password_changed", "user", user_id,
                  {"actor_user_id": current_user["id"], "target_user_id": user_id})
        # Revoke all existing sessions for the target user so old credentials can't be reused
        sessions = db.execute(
            text("SELECT token_jti FROM active_sessions WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchall()
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for session in sessions:
            db.execute(
                text("INSERT OR IGNORE INTO token_blacklist (jti, expires_at) VALUES (:jti, :exp)"),
                {"jti": session.token_jti, "exp": expiry},
            )
        db.execute(text("DELETE FROM active_sessions WHERE user_id = :uid"), {"uid": user_id})
        db.commit()
    return {"status": "updated"}

@router.delete("/users/{user_id}", tags=["Users"])
async def delete_user(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if current_user["id"] == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    # Prevent deleting the last admin account
    target = db.execute(text("SELECT role FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role == "admin":
        admin_count = db.execute(text("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1")).scalar()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last admin account")
    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    db.commit()
    return {"status": "deleted"}


@router.post("/users/import", tags=["Users"])
async def import_users_csv(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Bulk import users from a CSV file. Admin only.

    CSV columns: username, email, password, role (optional, defaults to 'viewer').
    Skips rows that duplicate an existing username. Validates password complexity.
    Respects license user limits.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")

    content = await file.read()
    try:
        text_content = content.decode("utf-8-sig")  # handle BOM
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text_content))

    # Validate header
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV file is empty or has no header row")
    lower_fields = [f.strip().lower() for f in reader.fieldnames]
    if "username" not in lower_fields or "email" not in lower_fields or "password" not in lower_fields:
        raise HTTPException(
            status_code=400,
            detail="CSV must have columns: username, email, password (and optionally role)",
        )

    # Normalise header keys to lowercase
    valid_roles = {"admin", "operator", "viewer"}
    email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    # Pre-fetch existing usernames for fast lookup
    existing = {
        r[0]
        for r in db.execute(text("SELECT username FROM users")).fetchall()
    }

    current_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar()

    created = 0
    skipped = 0
    errors = []

    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is header
        # Normalise keys
        row = {k.strip().lower(): (v.strip() if v else "") for k, v in raw_row.items()}

        username = row.get("username", "")
        email = row.get("email", "")
        password = row.get("password", "")
        role = row.get("role", "").lower() or "viewer"

        # --- Validation ---
        if not username:
            errors.append({"row": row_num, "reason": "Missing username"})
            continue
        if not email:
            errors.append({"row": row_num, "reason": "Missing email"})
            continue
        if not email_re.match(email):
            errors.append({"row": row_num, "reason": f"Invalid email format: {email}"})
            continue
        if not password:
            errors.append({"row": row_num, "reason": "Missing password"})
            continue

        pw_valid, pw_msg = _validate_password(password)
        if not pw_valid:
            errors.append({"row": row_num, "reason": pw_msg})
            continue

        if role not in valid_roles:
            errors.append({"row": row_num, "reason": f"Invalid role '{role}'. Must be admin, operator, or viewer"})
            continue

        # Duplicate check
        if username in existing:
            skipped += 1
            continue

        # License limit check
        try:
            check_user_limit(current_count)
        except HTTPException:
            errors.append({"row": row_num, "reason": "License user limit reached"})
            break  # stop processing further rows

        # Create user
        password_hash_val = hash_password(password)
        try:
            db.execute(
                text("""INSERT INTO users (username, email, password_hash, role)
                        VALUES (:username, :email, :password_hash, :role)"""),
                {"username": username, "email": email, "password_hash": password_hash_val, "role": role},
            )
            db.commit()
            existing.add(username)
            current_count += 1
            created += 1
        except Exception:
            db.rollback()
            skipped += 1  # likely unique constraint on email

    log_audit(db, "users_imported", "user", details=f"CSV import: {created} created, {skipped} skipped, {len(errors)} errors")

    return {"created": created, "skipped": skipped, "errors": errors}


# ============== Groups (Education/Enterprise) ==============

@router.get("/groups", tags=["Groups"])
async def list_groups(current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    require_feature("user_groups")
    # Non-admin callers only see their own group
    if current_user.get("role") == "admin":
        groups = db.execute(text("""
            SELECT g.id, g.name, g.description, g.owner_id, g.created_at, g.updated_at,
                   u.username AS owner_username,
                   (SELECT COUNT(*) FROM users WHERE group_id = g.id) AS member_count
            FROM groups g
            LEFT JOIN users u ON u.id = g.owner_id
            ORDER BY g.name
        """)).fetchall()
    else:
        user_group_id = current_user.get("group_id")
        if user_group_id is None:
            return []
        groups = db.execute(text("""
            SELECT g.id, g.name, g.description, g.owner_id, g.created_at, g.updated_at,
                   u.username AS owner_username,
                   (SELECT COUNT(*) FROM users WHERE group_id = g.id) AS member_count
            FROM groups g
            LEFT JOIN users u ON u.id = g.owner_id
            WHERE g.id = :gid
            ORDER BY g.name
        """), {"gid": user_group_id}).fetchall()
    return [dict(g._mapping) for g in groups]


@router.post("/groups", tags=["Groups"])
async def create_group(body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    require_feature("user_groups")
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    description = body.get("description", "").strip() or None
    owner_id = body.get("owner_id")

    if owner_id:
        owner = db.execute(text("SELECT role FROM users WHERE id = :id AND is_active = 1"), {"id": owner_id}).fetchone()
        if not owner:
            raise HTTPException(status_code=400, detail="Owner not found")
        if owner.role not in ("operator", "admin"):
            raise HTTPException(status_code=400, detail="Group owner must be an operator or admin")

    try:
        result = db.execute(text("""
            INSERT INTO groups (name, description, owner_id) VALUES (:name, :description, :owner_id)
        """), {"name": name, "description": description, "owner_id": owner_id})
        db.commit()
        return {"status": "created", "id": result.lastrowid}
    except Exception:
        raise HTTPException(status_code=400, detail="Group name already exists")


@router.get("/groups/{group_id}", tags=["Groups"])
async def get_group(group_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    require_feature("user_groups")
    # Non-admin callers can only view their own group (IDOR prevention)
    if current_user.get("role") != "admin" and current_user.get("group_id") != group_id:
        raise HTTPException(status_code=403, detail="Access denied")
    group = db.execute(text("""
        SELECT g.id, g.name, g.description, g.owner_id, g.created_at, g.updated_at,
               u.username AS owner_username
        FROM groups g
        LEFT JOIN users u ON u.id = g.owner_id
        WHERE g.id = :id
    """), {"id": group_id}).fetchone()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    members = db.execute(text(
        "SELECT id, username, email, role FROM users WHERE group_id = :gid"
    ), {"gid": group_id}).fetchall()

    result = dict(group._mapping)
    result["members"] = [dict(m._mapping) for m in members]
    return result


@router.patch("/groups/{group_id}", tags=["Groups"])
async def update_group(group_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    require_feature("user_groups")
    existing = db.execute(text("SELECT id FROM groups WHERE id = :id"), {"id": group_id}).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Group not found")

    ALLOWED_GROUP_FIELDS = {"name", "description", "owner_id"}
    updates = {k: v for k, v in body.items() if k in ALLOWED_GROUP_FIELDS}

    if "owner_id" in updates and updates["owner_id"]:
        owner = db.execute(text("SELECT role FROM users WHERE id = :id AND is_active = 1"), {"id": updates["owner_id"]}).fetchone()
        if not owner:
            raise HTTPException(status_code=400, detail="Owner not found")
        if owner.role not in ("operator", "admin"):
            raise HTTPException(status_code=400, detail="Group owner must be an operator or admin")

    if updates:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
        updates["id"] = group_id
        db.execute(text(f"UPDATE groups SET {set_clause} WHERE id = :id"), updates)
        db.commit()
    return {"status": "updated"}


@router.delete("/groups/{group_id}", tags=["Groups"])
async def delete_group(group_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    require_feature("user_groups")
    existing = db.execute(text("SELECT id FROM groups WHERE id = :id"), {"id": group_id}).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Group not found")
    # Unassign members first
    db.execute(text("UPDATE users SET group_id = NULL WHERE group_id = :gid"), {"gid": group_id})
    db.execute(text("DELETE FROM groups WHERE id = :id"), {"id": group_id})
    db.commit()
    return {"status": "deleted"}


# =============================================================================
# RBAC Permissions
# =============================================================================

RBAC_DEFAULT_PAGE_ACCESS = {
    "dashboard": ["admin", "operator", "viewer"],
    "timeline": ["admin", "operator", "viewer"],
    "jobs": ["admin", "operator", "viewer"],
    "printers": ["admin", "operator", "viewer"],
    "models": ["admin", "operator", "viewer"],
    "spools": ["admin", "operator", "viewer"],
    "cameras": ["admin", "operator", "viewer"],
    "analytics": ["admin", "operator", "viewer"],
    "calculator": ["admin", "operator", "viewer"],
    "upload": ["admin", "operator"],
    "maintenance": ["admin", "operator"],
    "settings": ["admin"],
    "admin": ["admin"],
    "branding": ["admin"],
    "education_reports": ["admin", "operator"],
    "orders": ["admin", "operator", "viewer"],
    "products": ["admin", "operator", "viewer"],
    "alerts": ["admin", "operator", "viewer"],
}

RBAC_DEFAULT_ACTION_ACCESS = {
    "jobs.create": ["admin", "operator"],
    "jobs.edit": ["admin", "operator"],
    "jobs.cancel": ["admin", "operator"],
    "jobs.delete": ["admin", "operator"],
    "jobs.start": ["admin", "operator"],
    "jobs.complete": ["admin", "operator"],
    "printers.add": ["admin"],
    "printers.edit": ["admin", "operator"],
    "printers.delete": ["admin"],
    "printers.slots": ["admin", "operator"],
    "printers.reorder": ["admin", "operator"],
    "models.create": ["admin", "operator"],
    "models.edit": ["admin", "operator"],
    "models.delete": ["admin"],
    "spools.edit": ["admin", "operator"],
    "spools.delete": ["admin"],
    "timeline.move": ["admin", "operator"],
    "upload.upload": ["admin", "operator"],
    "upload.schedule": ["admin", "operator"],
    "upload.delete": ["admin", "operator"],
    "maintenance.log": ["admin", "operator"],
    "maintenance.tasks": ["admin"],
    "dashboard.actions": ["admin", "operator"],
    "orders.create": ["admin", "operator"],
    "orders.edit": ["admin"],
    "orders.delete": ["admin", "operator"],
    "orders.ship": ["admin", "operator"],
    "products.create": ["admin", "operator"],
    "products.edit": ["admin", "operator"],
    "products.delete": ["admin"],
    "jobs.approve": ["admin", "operator"],
    "jobs.reject": ["admin", "operator"],
    "jobs.resubmit": ["admin", "operator", "viewer"],
    "alerts.read": ["admin", "operator", "viewer"],
    "printers.plug": ["admin", "operator"],
}


def _get_rbac(db: Session):
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row and row.value:
        data = row.value
        # Merge: defaults first, then DB overrides — ensures new keys always appear
        page = {**RBAC_DEFAULT_PAGE_ACCESS, **data.get("page_access", {})}
        action = {**RBAC_DEFAULT_ACTION_ACCESS, **data.get("action_access", {})}
        return {"page_access": page, "action_access": action}
    return {
        "page_access": RBAC_DEFAULT_PAGE_ACCESS,
        "action_access": RBAC_DEFAULT_ACTION_ACCESS,
    }


@router.get("/permissions", tags=["RBAC"])
def get_permissions(db: Session = Depends(get_db)):
    """Get current RBAC permission map. Public (needed at login)."""
    return _get_rbac(db)


class RBACUpdateRequest(PydanticBaseModel):
    page_access: dict
    action_access: dict


@router.put("/permissions", tags=["RBAC"])
def update_permissions(data: RBACUpdateRequest, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update RBAC permissions. Admin only."""
    valid_roles = {"admin", "operator", "viewer"}
    for key, roles in data.page_access.items():
        if not isinstance(roles, list):
            raise HTTPException(400, f"page_access.{key} must be a list")
        for r in roles:
            if r not in valid_roles:
                raise HTTPException(400, f"Invalid role '{r}' in page_access.{key}")
        if key in ("admin", "settings") and "admin" not in roles:
            raise HTTPException(400, f"Cannot remove admin from '{key}' page")

    for key, roles in data.action_access.items():
        if not isinstance(roles, list):
            raise HTTPException(400, f"action_access.{key} must be a list")
        for r in roles:
            if r not in valid_roles:
                raise HTTPException(400, f"Invalid role '{r}' in action_access.{key}")

    value = {"page_access": data.page_access, "action_access": data.action_access}
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row:
        row.value = value
    else:
        row = SystemConfig(key="rbac_permissions", value=value)
        db.add(row)
    db.commit()
    return {"message": "Permissions updated", **value}


@router.post("/permissions/reset", tags=["RBAC"])
def reset_permissions(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Reset permissions to defaults. Admin only."""
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row:
        db.delete(row)
        db.commit()
    return {
        "message": "Reset to defaults",
        "page_access": RBAC_DEFAULT_PAGE_ACCESS,
        "action_access": RBAC_DEFAULT_ACTION_ACCESS,
    }


# =============================================================================
# Self-Service Password Reset
# =============================================================================

class ForgotPasswordRequest(PydanticBaseModel):
    email: str

class ResetPasswordRequest(PydanticBaseModel):
    token: str
    new_password: str


@router.post("/auth/forgot-password", tags=["Auth"])
async def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Request a password reset link. Always returns 200 to prevent user enumeration."""
    import secrets as _secrets

    # Check SMTP is configured
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    smtp = config.value if config else {}
    if not smtp.get("enabled") or not smtp.get("host"):
        raise HTTPException(status_code=503, detail="Password reset requires SMTP to be configured.")

    # Find user by email — always return 200
    user_row = db.execute(
        text("SELECT id, username FROM users WHERE email = :email AND is_active = 1"),
        {"email": body.email},
    ).fetchone()

    if user_row:
        token = _secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.execute(
            text("INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (:uid, :tok, :exp)"),
            {"uid": user_row[0], "tok": token, "exp": expires},
        )
        db.commit()

        # Determine the base URL
        base_url_row = db.execute(
            text("SELECT value FROM system_config WHERE key = 'base_url'")
        ).fetchone()
        base_url = base_url_row[0].strip('"') if base_url_row else ""

        reset_link = f"{base_url}/reset-password?token={token}"
        html = f"""
        <html><body style="font-family: Arial, sans-serif; padding: 20px; background: #1a1a1a; color: #e0e0e0;">
        <div style="max-width: 500px; margin: 0 auto; background: #2a2a2a; padding: 20px; border-radius: 8px;">
            <h2 style="color: #3b82f6; margin-top: 0;">Password Reset — O.D.I.N.</h2>
            <p>A password reset was requested for your account <strong>{user_row[1]}</strong>.</p>
            <p><a href="{reset_link}" style="color: #3b82f6;">Click here to reset your password</a></p>
            <p style="color: #888; font-size: 12px;">This link expires in 1 hour. If you didn't request this, ignore this email.</p>
            <hr style="border: none; border-top: 1px solid #444; margin: 20px 0;">
            <p style="color: #888; font-size: 12px;">O.D.I.N.</p>
        </div></body></html>
        """
        _send_odin_email(db, body.email, "O.D.I.N. Password Reset", html)

    return {"status": "ok", "message": "If that email is registered, a reset link has been sent."}


@router.post("/auth/reset-password", tags=["Auth"])
async def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Reset password using a valid token."""
    row = db.execute(
        text("SELECT id, user_id, expires_at, used FROM password_reset_tokens WHERE token = :tok"),
        {"tok": body.token},
    ).fetchone()

    if not row or row[3]:  # not found or already used
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    if datetime.fromisoformat(row[2]) < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    # Validate password
    pw_valid, pw_msg = _validate_password(body.new_password)
    if not pw_valid:
        raise HTTPException(status_code=400, detail=pw_msg)

    # Update password
    user_id = row[1]
    password_hash = hash_password(body.new_password)
    db.execute(text("UPDATE users SET password_hash = :h WHERE id = :id"), {"h": password_hash, "id": user_id})

    # Mark token used
    db.execute(text("UPDATE password_reset_tokens SET used = 1 WHERE id = :id"), {"id": row[0]})

    # Blacklist all existing sessions
    sessions = db.execute(
        text("SELECT token_jti FROM active_sessions WHERE user_id = :uid"), {"uid": user_id}
    ).fetchall()
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    for s in sessions:
        db.execute(
            text("INSERT OR IGNORE INTO token_blacklist (jti, expires_at) VALUES (:jti, :exp)"),
            {"jti": s[0], "exp": expiry},
        )
    db.execute(text("DELETE FROM active_sessions WHERE user_id = :uid"), {"uid": user_id})
    db.commit()

    return {"status": "ok", "message": "Password updated. Please log in."}


@router.get("/auth/capabilities", tags=["Auth"])
async def auth_capabilities(db: Session = Depends(get_db)):
    """Public endpoint to check available auth features (SMTP, OIDC, etc.)."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    smtp = config.value if config else {}
    smtp_enabled = bool(smtp.get("enabled") and smtp.get("host"))
    return {"smtp_enabled": smtp_enabled}
