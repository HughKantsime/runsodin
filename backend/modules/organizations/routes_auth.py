"""Organizations auth routes — login, logout, MFA, me/theme, ws-token, password reset, auth capabilities."""

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role
from core.auth_helpers import (
    _validate_password, _check_rate_limit, _record_login_attempt, _is_locked_out
)
import core.auth as auth_module
from core.auth import hash_password, create_access_token, verify_password, decode_token
from core.models import SystemConfig
from core.config import settings as _settings
from core.rate_limit import limiter

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Session helpers ==============

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


# ============== Login ==============

@router.post("/auth/login", tags=["Auth"])
@limiter.limit("10/minute")
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
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

    if user.mfa_enabled:
        mfa_token = create_access_token(
            data={"sub": user.username, "role": user.role, "mfa_pending": True},
            expires_delta=timedelta(minutes=5)
        )
        return {"access_token": mfa_token, "token_type": "bearer", "mfa_required": True}

    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    _record_session(db, user.id, access_token, client_ip, request.headers.get("user-agent", ""))
    log_audit(db, "auth.login", "user", user.id, details={"username": user.username}, ip=client_ip)

    from fastapi.responses import JSONResponse
    resp = JSONResponse({"access_token": access_token, "token_type": "bearer"})
    resp.set_cookie(
        key="session", value=access_token, httponly=True,
        secure=_settings.cookie_secure, samesite=_settings.cookie_samesite,
        path="/", max_age=86400,
    )
    return resp


# ============== Logout ==============

@router.post("/auth/logout", tags=["Auth"])
async def logout(request: Request, response: Response, db: Session = Depends(get_db),
                 current_user: dict = Depends(get_current_user)):
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
                db.execute(text("DELETE FROM active_sessions WHERE token_jti = :jti"), {"jti": jti})
        except Exception:
            log.debug("Could not blacklist token on logout", exc_info=True)

    session_token = request.cookies.get("session")
    if session_token:
        _blacklist_token(session_token)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]
        if bearer_token != session_token:
            _blacklist_token(bearer_token)

    db.commit()
    response.delete_cookie(key="session", path="/")
    return {"detail": "Logged out"}


# ============== MFA ==============

@router.post("/auth/mfa/verify", tags=["Auth"])
@limiter.limit("10/minute")
async def mfa_verify(request: Request, body: dict, db: Session = Depends(get_db)):
    """Verify TOTP code during login. Requires mfa_pending token."""
    import pyotp
    import jwt as _jwt

    mfa_token = body.get("mfa_token", "")
    code = body.get("code", "")
    if not mfa_token or not code:
        raise HTTPException(status_code=400, detail="mfa_token and code are required")

    token_data = decode_token(mfa_token)
    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA token")

    payload = _jwt.decode(mfa_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
    if not payload.get("mfa_pending"):
        raise HTTPException(status_code=400, detail="Token is not an MFA challenge token")

    mfa_pending_jti = payload.get("jti")
    if mfa_pending_jti:
        blacklisted = db.execute(
            text("SELECT 1 FROM token_blacklist WHERE jti = :jti"), {"jti": mfa_pending_jti}
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

    mfa_jti = payload.get("jti")
    if mfa_jti:
        mfa_exp = payload.get("exp", 0)
        db.execute(text("INSERT OR IGNORE INTO token_blacklist (jti, expires_at) VALUES (:jti, :exp)"),
                   {"jti": mfa_jti, "exp": datetime.fromtimestamp(mfa_exp, tz=timezone.utc).isoformat()})
        db.commit()

    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    client_ip = request.client.host if hasattr(request, 'client') and request.client else "unknown"
    _record_session(db, user.id, access_token, client_ip, request.headers.get("user-agent", ""))

    from fastapi.responses import JSONResponse
    resp = JSONResponse({"access_token": access_token, "token_type": "bearer"})
    resp.set_cookie(key="session", value=access_token, httponly=True,
                    secure=_settings.cookie_secure, samesite=_settings.cookie_samesite,
                    path="/", max_age=86400)
    return resp


@router.post("/auth/mfa/setup", tags=["Auth"])
async def mfa_setup(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Generate TOTP secret and provisioning URI for MFA setup."""
    import pyotp, qrcode, io, base64

    if current_user.get("mfa_enabled"):
        raise HTTPException(status_code=400, detail="MFA is already enabled. Disable it first.")

    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=current_user["username"], issuer_name="O.D.I.N.")

    img = qrcode.make(provisioning_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    from core.crypto import encrypt, get_fernet
    if not get_fernet():
        raise HTTPException(status_code=503, detail="Encryption is not configured on this server. MFA setup requires ENCRYPTION_KEY to be set.")
    encrypted_secret = encrypt(secret)
    db.execute(text("UPDATE users SET mfa_secret = :secret WHERE id = :id"),
               {"secret": encrypted_secret, "id": current_user["id"]})
    db.commit()

    return {"secret": secret, "provisioning_uri": provisioning_uri, "qr_code": f"data:image/png;base64,{qr_b64}"}


@router.post("/auth/mfa/confirm", tags=["Auth"])
async def mfa_confirm(body: dict, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Confirm MFA setup by verifying a TOTP code. Enables MFA on the account."""
    import pyotp

    code = body.get("code", "")
    if not code:
        raise HTTPException(status_code=400, detail="TOTP code is required")

    user = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": current_user["id"]}).fetchone()
    if not user or not user.mfa_secret:
        raise HTTPException(status_code=400, detail="Run /api/auth/mfa/setup first")
    if user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is already enabled")

    from core.crypto import decrypt
    secret = decrypt(user.mfa_secret)
    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code. Scan the QR code and try again.")

    db.execute(text("UPDATE users SET mfa_enabled = 1 WHERE id = :id"), {"id": current_user["id"]})
    db.commit()
    log_audit(db, "mfa_enabled", "user", current_user["id"], "MFA enabled")
    return {"status": "ok", "message": "MFA enabled successfully"}


@router.delete("/auth/mfa", tags=["Auth"])
async def mfa_disable(body: dict = None, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Disable MFA. Requires current TOTP code or admin role."""
    import pyotp

    user = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": current_user["id"]}).fetchone()
    if not user or not user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is not enabled")

    code = (body or {}).get("code", "")
    if code:
        from core.crypto import decrypt
        secret = decrypt(user.mfa_secret)
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            raise HTTPException(status_code=401, detail="Invalid MFA code")
    else:
        raise HTTPException(status_code=400, detail="TOTP code is required to disable MFA")

    db.execute(text("UPDATE users SET mfa_enabled = 0, mfa_secret = NULL WHERE id = :id"), {"id": current_user["id"]})
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

    db.execute(text("UPDATE users SET mfa_enabled = 0, mfa_secret = NULL WHERE id = :id"), {"id": user_id})
    db.commit()
    log_audit(db, "mfa_disabled_admin", "user", user_id, f"Admin force-disabled MFA for user {user.username}")
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
    db.execute(text("INSERT INTO system_config (key, value) VALUES ('require_mfa', :val) ON CONFLICT(key) DO UPDATE SET value = :val"),
               {"val": "true" if require else "false"})
    db.commit()
    return {"require_mfa": require}


# ============== Me / Theme / WS token ==============

@router.get("/auth/me", tags=["Auth"])
async def get_me(current_user: dict = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": current_user["username"], "email": current_user["email"],
            "role": current_user["role"], "group_id": current_user.get("group_id")}


@router.get("/auth/me/theme", tags=["Auth"])
async def get_theme(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get the current user's theme preferences."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    row = db.execute(text("SELECT theme_json FROM users WHERE id = :id"), {"id": current_user["id"]}).fetchone()
    if row and row[0]:
        try:
            import json as _json
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
    db.execute(text("UPDATE users SET theme_json = :t WHERE id = :id"), {"t": theme_json, "id": current_user["id"]})
    db.commit()
    return body


@router.post("/auth/ws-token", tags=["Auth"])
async def get_ws_token(current_user: dict = Depends(get_current_user)):
    """Issue a short-lived JWT for WebSocket authentication."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ws_token = create_access_token(
        data={"sub": current_user["username"], "role": current_user["role"], "ws": True},
        expires_delta=timedelta(minutes=5),
    )
    return {"token": ws_token}


# ============== Password reset ==============

class ForgotPasswordRequest(PydanticBaseModel):
    email: str


class ResetPasswordRequest(PydanticBaseModel):
    token: str
    new_password: str


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


@router.post("/auth/forgot-password", tags=["Auth"])
async def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Request a password reset link. Always returns 200 to prevent user enumeration."""
    import secrets as _secrets

    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    smtp = config.value if config else {}
    if not smtp.get("enabled") or not smtp.get("host"):
        raise HTTPException(status_code=503, detail="Password reset requires SMTP to be configured.")

    user_row = db.execute(
        text("SELECT id, username FROM users WHERE email = :email AND is_active = 1"),
        {"email": body.email},
    ).fetchone()

    if user_row:
        token = _secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.execute(text("INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (:uid, :tok, :exp)"),
                   {"uid": user_row[0], "tok": token, "exp": expires})
        db.commit()

        base_url_row = db.execute(text("SELECT value FROM system_config WHERE key = 'base_url'")).fetchone()
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

    if not row or row[3]:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    if datetime.fromisoformat(row[2]) < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    pw_valid, pw_msg = _validate_password(body.new_password)
    if not pw_valid:
        raise HTTPException(status_code=400, detail=pw_msg)

    user_id = row[1]
    password_hash = hash_password(body.new_password)
    db.execute(text("UPDATE users SET password_hash = :h WHERE id = :id"), {"h": password_hash, "id": user_id})
    db.execute(text("UPDATE password_reset_tokens SET used = 1 WHERE id = :id"), {"id": row[0]})

    sessions = db.execute(text("SELECT token_jti FROM active_sessions WHERE user_id = :uid"), {"uid": user_id}).fetchall()
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    for s in sessions:
        db.execute(text("INSERT OR IGNORE INTO token_blacklist (jti, expires_at) VALUES (:jti, :exp)"),
                   {"jti": s[0], "exp": expiry})
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
