"""Organizations auth routes — login, logout, MFA, me/theme, ws-token, password reset, auth capabilities."""

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from typing import Literal, Optional

from pydantic import BaseModel as PydanticBaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.audit_actor import bind_verified_actor
from core.db import get_db
from core.db_compat import sql
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, require_scope, handler_enforces_token_scope, require_interactive_session
from core.auth_helpers import (
    _validate_password, _check_rate_limit, _record_login_attempt, _is_locked_out
)
import core.auth as auth_module
from core.auth import hash_password, create_access_token, verify_password, decode_token
from core.models import SystemConfig
from core.config import settings as _settings
from core.rate_limit import limiter
from modules.organizations.education_access import capability_snapshot_id

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Session helpers ==============

from core.session_tracking import record_session as _record_session


# ============== Login ==============

@router.post("/auth/login", tags=["Auth"])
@limiter.limit("10/minute")
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    client_ip = request.client.host if hasattr(request, 'client') and request.client else "unknown"
    if _check_rate_limit(db, client_ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 5 minutes.")
    if _is_locked_out(db, form_data.username):
        raise HTTPException(status_code=423, detail="Account temporarily locked due to repeated failed attempts. Try again in 15 minutes.")

    user = db.execute(text("SELECT * FROM users WHERE username = :username"),
                      {"username": form_data.username}).fetchone()
    if not user or not verify_password(form_data.password, user.password_hash):
        if _record_login_attempt(db, client_ip, form_data.username, False) is not True:
            raise HTTPException(status_code=503, detail="Login temporarily unavailable. Try again later.")
        log_audit(db, "auth.login_failed", "user", details={"username": form_data.username}, ip=client_ip)
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled")

    if _record_login_attempt(db, client_ip, form_data.username, True) is not True:
        raise HTTPException(status_code=503, detail="Login temporarily unavailable. Try again later.")
    db.execute(text("UPDATE users SET last_login = :now WHERE id = :id"),
               {"now": datetime.now(timezone.utc), "id": user.id})
    db.commit()

    from core.mfa_login import login_mfa_response
    continuation = login_mfa_response(db, user.id, expected_password_hash=user.password_hash)
    if continuation is not None:
        return continuation

    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    if _record_session(db, user.id, access_token, client_ip, request.headers.get("user-agent", "")) is not True:
        raise HTTPException(status_code=503, detail="Login temporarily unavailable. Try again later.")
    bind_verified_actor(db, dict(user._mapping), "password_login")
    log_audit(db, "auth.login", "user", user.id, details={"username": user.username}, ip=client_ip)
    db.commit()

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
                    text(f"{sql.insert_or_ignore_prefix()} token_blacklist (jti, expires_at) VALUES (:jti, :exp){sql.on_conflict_ignore('jti')}"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
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

    if current_user:
        log_audit(db, "auth.logout", "user", current_user.get("id"),
                  details={"username": current_user.get("username")})
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

    import math
    try:
        payload = _jwt.decode(mfa_token, auth_module.SECRET_KEY,
                              algorithms=[auth_module.ALGORITHM],
                              options={"require": ["exp", "sub", "jti"]})
        expiry = payload["exp"]
        if (not isinstance(payload["sub"], str) or not payload["sub"]
                or not isinstance(payload["jti"], str) or not payload["jti"]
                or payload.get("mfa_pending") is not True
                or ("role" in payload and not isinstance(payload["role"], str))
                or "mfa_setup_required" in payload
                or type(expiry) not in (int, float) or not math.isfinite(expiry)):
            raise ValueError("Invalid challenge")
        expiry_datetime = datetime.fromtimestamp(expiry, tz=timezone.utc)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA token") from None

    mfa_pending_jti = payload.get("jti")
    if mfa_pending_jti:
        blacklisted = db.execute(
            text("SELECT 1 FROM token_blacklist WHERE jti = :jti"), {"jti": mfa_pending_jti}
        ).fetchone()
        if blacklisted:
            raise HTTPException(status_code=401, detail="MFA token has already been used")

    user = db.execute(text("SELECT * FROM users WHERE username = :username"),
                      {"username": payload["sub"]}).fetchone()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid MFA challenge")
    if not user.mfa_enabled or not user.mfa_secret:
        raise HTTPException(status_code=400, detail="MFA not configured for this user")

    from core.crypto import decrypt
    secret = decrypt(user.mfa_secret)
    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid MFA code")

    if datetime.now(timezone.utc) >= expiry_datetime:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA token")
    try:
        consumption = db.execute(text(
            f"{sql.insert_or_ignore_prefix()} token_blacklist (jti, expires_at) "
            f"VALUES (:jti, :exp){sql.on_conflict_ignore('jti')}"),
            {"jti": payload["jti"], "exp": expiry_datetime.isoformat()})
        claimed = consumption.rowcount == 1
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="Login temporarily unavailable. Try again later.") from None
    if not claimed:
        raise HTTPException(status_code=401, detail="MFA token has already been used")

    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    client_ip = request.client.host if hasattr(request, 'client') and request.client else "unknown"
    if _record_session(db, user.id, access_token, client_ip, request.headers.get("user-agent", "")) is not True:
        raise HTTPException(status_code=503, detail="Login temporarily unavailable. Try again later.")

    bind_verified_actor(db, dict(user._mapping), "mfa_login")
    log_audit(db, "auth.login", "user", user.id, ip=client_ip)
    db.commit()

    from fastapi.responses import JSONResponse
    resp = JSONResponse({"access_token": access_token, "token_type": "bearer"})
    resp.set_cookie(key="session", value=access_token, httponly=True,
                    secure=_settings.cookie_secure, samesite=_settings.cookie_samesite,
                    path="/", max_age=86400)
    return resp


@router.post("/auth/mfa/setup", tags=["Auth"], dependencies=[Depends(require_interactive_session())])
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


@router.post("/auth/mfa/confirm", tags=["Auth"], dependencies=[Depends(require_interactive_session())])
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

    db.execute(text("UPDATE users SET mfa_enabled = TRUE WHERE id = :id"), {"id": current_user["id"]})
    log_audit(db, "mfa_enabled", "user", current_user["id"], "MFA enabled")
    db.commit()
    return {"status": "ok", "message": "MFA enabled successfully"}


@router.delete("/auth/mfa", tags=["Auth"], dependencies=[Depends(require_interactive_session())])
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

    db.execute(text("UPDATE users SET mfa_enabled = FALSE, mfa_secret = NULL WHERE id = :id"), {"id": current_user["id"]})
    log_audit(db, "mfa_disabled", "user", current_user["id"], "MFA disabled")
    db.commit()
    return {"status": "ok", "message": "MFA disabled"}


@router.delete("/admin/users/{user_id}/mfa", tags=["Auth"], dependencies=[Depends(require_interactive_session())])
async def admin_mfa_disable(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: force-disable MFA for a user (no TOTP required)."""
    user = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    global_admin = current_user.get("role") == "admin" and not current_user.get("group_id")
    if not global_admin and user.group_id != current_user.get("group_id"):
        raise HTTPException(status_code=403, detail="Cannot modify users outside your group")
    if not user.mfa_enabled:
        raise HTTPException(status_code=400, detail="MFA is not enabled for this user")
    try:
        params = {"id": user_id}
        statement = "UPDATE users SET mfa_enabled = FALSE, mfa_secret = NULL WHERE id = :id AND mfa_enabled = TRUE"
        if not global_admin:
            statement += " AND group_id = :group_id"
            params["group_id"] = current_user["group_id"]
        changed = db.execute(text(statement), params)
        if changed.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="User changed. Reload before retrying.")
        from core.mfa_login import cancel_pending_enrollments
        cancel_pending_enrollments(db, user_id)
        log_audit(db, "mfa_disabled_admin", "user", user_id, f"Admin force-disabled MFA for user {user.username}")
        db.commit()
    except HTTPException:
        raise
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="MFA change could not be confirmed. Check the user before retrying.") from None
    return {"status": "ok", "message": f"MFA disabled for {user.username}"}


@router.get("/auth/mfa/status", tags=["Auth"])
async def mfa_status(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get MFA status for the current user."""
    return {"mfa_enabled": bool(current_user.get("mfa_enabled"))}


@router.get("/config/require-mfa", tags=["Config"])
async def get_require_mfa(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get whether MFA is required for all users."""
    from core.mfa_login import read_required_mfa
    return {"require_mfa": read_required_mfa(db)}


class RequireMFARequest(PydanticBaseModel):
    require_mfa: bool = False


@router.put("/config/require-mfa", tags=["Config"], dependencies=[Depends(require_interactive_session())])
async def set_require_mfa(body: RequireMFARequest, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set whether MFA is required for all users."""
    require = body.require_mfa
    db.execute(text("INSERT INTO system_config (key, value) VALUES ('require_mfa', :val) ON CONFLICT(key) DO UPDATE SET value = :val"),
               {"val": "true" if require else "false"})
    db.commit()
    return {"require_mfa": require}


# ============== Me / Theme / WS token ==============

# Any valid credential may identify itself: a scoped token of any scope can read
# its own principal (the response describes only the caller).
@router.get("/auth/me", tags=["Auth"], dependencies=[Depends(handler_enforces_token_scope())])
async def get_me(current_user: dict = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"id": current_user["id"], "username": current_user["username"], "email": current_user["email"],
            "role": current_user["role"], "group_id": current_user.get("group_id")}


_DEFAULT_THEME = {"accent_color": "#6366f1", "sidebar_style": "dark", "background": "default"}


class ThemePreferences(PydanticBaseModel):
    """Per-user theme. Strictly validated: values are applied as CSS by the UI."""
    model_config = ConfigDict(extra="forbid")
    accent_color: Optional[str] = Field(None, pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
    sidebar_style: Optional[Literal["dark", "light"]] = None
    background: Optional[str] = Field(None, pattern=r"^[a-z0-9_-]{1,32}$")
    mode: Optional[Literal["light", "dark"]] = None

    def stored(self) -> dict:
        return self.model_dump(exclude_unset=True, exclude_none=True)

    @classmethod
    def lenient(cls, raw) -> dict:
        """Read path: keep each valid known field of a stored row, drop the rest."""
        kept = {}
        for key, value in (raw.items() if isinstance(raw, dict) else ()):
            if key in cls.model_fields:
                try:
                    kept.update(cls.model_validate({key: value}).stored())
                except Exception:
                    pass
        return kept


@router.get("/auth/me/theme", tags=["Auth"])
async def get_theme(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get the current user's theme preferences."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    row = db.execute(text("SELECT theme_json FROM users WHERE id = :id"), {"id": current_user["id"]}).fetchone()
    if row and row[0]:
        try:
            # Rows written before validation existed are re-validated field by field, never echoed raw.
            return {**_DEFAULT_THEME, **ThemePreferences.lenient(json.loads(row[0]))}
        except Exception as e:
            log.debug(f"Ignoring invalid stored theme JSON: {e}")
    return dict(_DEFAULT_THEME)


@router.put("/auth/me/theme", tags=["Auth"])
async def set_theme(prefs: ThemePreferences, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Set the current user's theme preferences (422 for unknown keys or invalid values)."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    stored = prefs.stored()
    db.execute(text("UPDATE users SET theme_json = :t WHERE id = :id"), {"t": json.dumps(stored), "id": current_user["id"]})
    db.commit()
    return stored


# A WebSocket token streams fleet telemetry, so scoped API tokens need the
# generic read scope to mint one.
@router.post("/auth/ws-token", tags=["Auth"], dependencies=[Depends(require_scope("read"))])
@limiter.limit("10/minute")
async def get_ws_token(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Issue a short-lived JWT for WebSocket authentication."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ws_token = create_access_token(
        data={
            "sub": current_user["username"],
            "role": current_user["role"],
            "user_id": current_user["id"],
            "group_id": current_user.get("group_id"),
            "capability_snapshot_id": capability_snapshot_id(db, current_user),
            "ws": True,
        },
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
        except Exception as e:
            log.debug(f"Failed to decrypt SMTP password (using raw): {e}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp.get("from_address", smtp.get("username", "odin@localhost"))
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        # v1.8.9 (codex pass 7): runtime ITAR guard.
        from core.itar import enforce_host_destination
        enforce_host_destination(smtp["host"], scheme="smtp")

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
@limiter.limit("5/minute")
async def forgot_password(request: Request, body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Request a password reset link. Always returns 200 to prevent user enumeration."""
    from modules.organizations.password_reset import reset_mail_base_url, send_reset_link

    base_url = reset_mail_base_url(db)
    user_row = db.execute(
        text("SELECT id, username FROM users WHERE email = :email AND is_active IS TRUE"),
        {"email": body.email},
    ).fetchone()
    if user_row:
        send_reset_link(db, user_row.id, user_row.username, body.email, _send_odin_email,
                        base_url=base_url)

    return {"status": "ok", "message": "If that email is registered, a reset link has been sent."}


@router.post("/auth/reset-password", tags=["Auth"])
@limiter.limit("5/minute")
async def reset_password(request: Request, body: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Reset password using a valid token."""
    def invalid():
        return HTTPException(status_code=400, detail="Invalid or expired reset token")

    def unexpired(value):
        try:
            expiry = datetime.fromisoformat(value) if isinstance(value, str) else value
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry.astimezone(timezone.utc) > datetime.now(timezone.utc)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False

    from modules.organizations.password_reset import reset_token_digest
    try:
        digest = reset_token_digest(body.token)
    except UnicodeError:
        raise invalid() from None
    lookup = text("SELECT id, user_id, expires_at, used FROM password_reset_tokens "
                  "WHERE token = :digest OR (token = :tok AND token NOT LIKE 'sha256:%')")
    row = db.execute(lookup, {"tok": body.token, "digest": digest}).fetchone()
    if not row or row.used or not unexpired(row.expires_at):
        raise invalid()

    pw_valid, pw_msg = _validate_password(body.new_password)
    if not pw_valid:
        raise HTTPException(status_code=400, detail=pw_msg)
    password_hash = hash_password(body.new_password)

    try:
        # Serialize resets on the account, including requests using different links.
        # A conditional no-op UPDATE takes the row lock on both supported databases.
        active_user = db.execute(text(
            "UPDATE users SET password_hash = password_hash WHERE id = :uid AND is_active IS TRUE"
        ), {"uid": row.user_id})
        if active_user.rowcount != 1:
            raise invalid()
        row = db.execute(lookup, {"tok": body.token, "digest": digest}).fetchone()
        if not row or row.used or not unexpired(row.expires_at):
            raise invalid()
        now = datetime.now(timezone.utc)
        expiry_predicate = (
            "julianday(expires_at) > julianday(:now)"
            if db.get_bind().dialect.name == "sqlite" else "expires_at > :now"
        )
        claimed = db.execute(text(
            "UPDATE password_reset_tokens SET used = 1 "
            "WHERE id = :id AND user_id = :uid AND used = 0 AND " + expiry_predicate
        ), {"id": row.id, "uid": row.user_id, "now": now.isoformat()}).rowcount
        if claimed != 1:
            raise invalid()
        changed = db.execute(text(
            "UPDATE users SET password_hash = :h WHERE id = :uid AND is_active IS TRUE"
        ), {"h": password_hash, "uid": row.user_id}).rowcount
        if changed != 1:
            raise invalid()
        db.execute(text("UPDATE password_reset_tokens SET used = 1 WHERE user_id = :uid AND used = 0"),
                   {"uid": row.user_id})
        from core.mfa_login import cancel_pending_enrollments
        from modules.organizations.password_reset import revoke_sessions_and_biometric
        cancel_pending_enrollments(db, row.user_id)
        # A reset revokes biometric tokens and every session (the user is not
        # authenticated here, so none is kept).
        revoke_sessions_and_biometric(db, row.user_id,
                                      blacklist_hours=auth_module.ACCESS_TOKEN_EXPIRE_HOURS)
        log_audit(db, "user.password_reset", "user", row.user_id,
                  {"target_user_id": row.user_id})
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"status": "ok", "message": "Password updated. Please log in."}


@router.get("/auth/capabilities", tags=["Auth"])
async def auth_capabilities(db: Session = Depends(get_db)):
    """Public endpoint to check available auth features (SMTP, OIDC, etc.)."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    smtp = config.value if config else {}
    smtp_enabled = bool(smtp.get("enabled") and smtp.get("host"))
    return {"smtp_enabled": smtp_enabled}
