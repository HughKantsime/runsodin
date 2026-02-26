"""Organizations OIDC routes — SSO/OIDC login, callback, token exchange, admin config."""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user
from core.rbac import require_role
import core.auth as auth_module
from core.auth import create_access_token
from core.config import settings as _settings

log = logging.getLogger("odin.api")
router = APIRouter()


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


# ============== OIDC public config ==============

@router.get("/auth/oidc/config", tags=["Auth"])
async def get_oidc_public_config(db: Session = Depends(get_db)):
    """Get public OIDC config for login page (is SSO enabled, display name)."""
    row = db.execute(text("SELECT is_enabled, display_name FROM oidc_config LIMIT 1")).fetchone()
    if not row:
        return {"enabled": False}
    return {"enabled": bool(row[0]), "display_name": row[1] or "Single Sign-On"}


# ============== OIDC login flow ==============

@router.get("/auth/oidc/login", tags=["Auth"])
async def oidc_login(request: Request, db: Session = Depends(get_db)):
    """Initiate OIDC login flow. Redirects to identity provider."""
    from modules.organizations.oidc_handler import create_handler_from_config
    row = db.execute(text("SELECT * FROM oidc_config WHERE is_enabled = 1 LIMIT 1")).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="OIDC not configured")
    config = dict(row._mapping)
    if _settings.oidc_redirect_uri:
        redirect_uri = _settings.oidc_redirect_uri
    else:
        base_url = str(request.base_url).rstrip("/")
        redirect_uri = f"{base_url}/api/auth/oidc/callback"
    handler = create_handler_from_config(config, redirect_uri)
    url, state = await handler.get_authorization_url()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=url, status_code=302)


# ============== OIDC callback ==============

@router.get("/auth/oidc/callback", tags=["Auth"])
async def oidc_callback(request: Request, code: str = None, state: str = None,
                        error: str = None, error_description: str = None, db: Session = Depends(get_db)):
    """Handle OIDC callback from identity provider."""
    from modules.organizations.oidc_handler import create_handler_from_config
    from fastapi.responses import RedirectResponse

    if error:
        log.error(f"OIDC error: {error} - {error_description}")
        return RedirectResponse(url=f"/?error={quote(str(error))}", status_code=302)
    if not code or not state:
        return RedirectResponse(url="/?error=missing_params", status_code=302)

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
    if not handler.validate_state(state):
        return RedirectResponse(url="/?error=invalid_state", status_code=302)

    try:
        tokens = await handler.exchange_code(code)
        id_token_claims = await handler.parse_id_token(tokens["id_token"])
        user_info = await handler.get_user_info(tokens["access_token"])
        oidc_subject = id_token_claims.get("sub") or id_token_claims.get("oid")
        email = user_info.get("mail") or user_info.get("userPrincipalName") or id_token_claims.get("email")

        if not oidc_subject or not email:
            log.error(f"Missing required claims: sub={oidc_subject}, email={email}")
            return RedirectResponse(url="/?error=missing_claims", status_code=302)

        oidc_provider = config.get("display_name", "oidc").lower().replace(" ", "_")
        existing = db.execute(
            text("SELECT * FROM users WHERE oidc_subject = :sub AND oidc_provider = :provider"),
            {"sub": oidc_subject, "provider": oidc_provider}
        ).fetchone()

        if existing:
            user_id = existing[0]
            db.execute(text("UPDATE users SET last_login = :now, email = :email WHERE id = :id"),
                       {"now": datetime.now(timezone.utc).isoformat(), "email": email, "id": user_id})
            db.commit()
            user_role = existing._mapping.get("role", "operator")
        elif config.get("auto_create_users", False):
            username = email.split("@")[0]
            default_role = config.get("default_role", "viewer")
            base_username = username
            counter = 1
            while db.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username}).fetchone():
                username = f"{base_username}{counter}"
                counter += 1
            db.execute(text("""
                INSERT INTO users (username, email, password_hash, role, oidc_subject, oidc_provider, last_login)
                VALUES (:username, :email, '', :role, :sub, :provider, :now)
            """), {"username": username, "email": email, "role": default_role,
                   "sub": oidc_subject, "provider": oidc_provider,
                   "now": datetime.now(timezone.utc).isoformat()})
            db.commit()
            user_id = db.execute(text("SELECT last_insert_rowid()")).fetchone()[0]
            user_role = default_role
            log.info(f"Created OIDC user: {username} ({email})")
        else:
            log.warning(f"OIDC user not found and auto-create disabled: {email}")
            return RedirectResponse(url="/?error=user_not_found", status_code=302)

        access_token = create_access_token(data={
            "sub": existing._mapping.get("username") if existing else username,
            "role": user_role,
        })

        import secrets as _secrets
        from core.crypto import encrypt as _crypto_encrypt
        oidc_code = _secrets.token_urlsafe(48)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        db.execute(text("INSERT INTO oidc_auth_codes (code, access_token, expires_at) VALUES (:code, :token, :exp)"),
                   {"code": oidc_code, "token": _crypto_encrypt(access_token), "exp": expires_at})
        db.commit()
        return RedirectResponse(url=f"/?oidc_code={oidc_code}", status_code=302)

    except Exception as e:
        log.error(f"OIDC callback error: {e}", exc_info=True)
        return RedirectResponse(url=f"/?error=auth_failed", status_code=302)


# ============== OIDC code exchange ==============

@router.post("/auth/oidc/exchange", tags=["Auth"])
async def oidc_exchange_code(body: dict, request: Request, db: Session = Depends(get_db)):
    """Exchange a one-time OIDC auth code for a JWT access token."""
    code = body.get("code", "")
    if not code:
        raise HTTPException(status_code=400, detail="code is required")

    row = db.execute(text("SELECT access_token, expires_at FROM oidc_auth_codes WHERE code = :code"),
                     {"code": code}).fetchone()
    db.execute(text("DELETE FROM oidc_auth_codes WHERE code = :code"), {"code": code})
    db.execute(text("DELETE FROM oidc_auth_codes WHERE expires_at < :now"),
               {"now": datetime.now(timezone.utc).isoformat()})
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

    from core.crypto import decrypt as _crypto_decrypt, is_encrypted as _crypto_is_encrypted
    raw_token = row.access_token
    access_token = _crypto_decrypt(raw_token) if _crypto_is_encrypted(raw_token) else raw_token

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
    resp.set_cookie(key="session", value=access_token, httponly=True,
                    secure=_settings.cookie_secure, samesite=_settings.cookie_samesite,
                    path="/", max_age=86400)
    return resp


# ============== Admin OIDC config ==============

@router.get("/admin/oidc", tags=["Admin"])
async def get_oidc_config(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get full OIDC configuration (admin only)."""
    row = db.execute(text("SELECT * FROM oidc_config LIMIT 1")).fetchone()
    if not row:
        return {"configured": False}
    config = dict(row._mapping)
    if "client_secret_encrypted" in config:
        config["has_client_secret"] = bool(config["client_secret_encrypted"])
        del config["client_secret_encrypted"]
    return config


@router.put("/admin/oidc", tags=["Admin"])
async def update_oidc_config(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update OIDC configuration (admin only)."""
    data = await request.json()
    client_secret = data.get("client_secret")
    if client_secret:
        from core.crypto import encrypt
        data["client_secret_encrypted"] = encrypt(client_secret)
        del data["client_secret"]

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
