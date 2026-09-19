"""Organizations OIDC routes — SSO/OIDC login, callback, token exchange, admin config."""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import execute_insert_returning_id, sql
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, require_superadmin
import core.auth as auth_module
from core.auth import create_access_token
from core.config import settings as _settings

log = logging.getLogger("odin.api")
router = APIRouter()


class OIDCIdentityError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _resolve_oidc_user(db: Session, config: dict, claims: dict, user_info: dict) -> dict:
    """Resolve, bind, or create one exact issuer+subject identity."""
    issuer = claims.get("iss")
    subject = claims.get("sub")
    email = (
        user_info.get("mail")
        or user_info.get("userPrincipalName")
        or claims.get("email")
    )
    if not isinstance(issuer, str) or not issuer:
        raise OIDCIdentityError("missing_issuer")
    if not isinstance(subject, str) or not subject:
        raise OIDCIdentityError("missing_subject")
    if not isinstance(email, str) or not email:
        raise OIDCIdentityError("missing_email")

    provider = config.get("display_name", "oidc").lower().replace(" ", "_")
    existing = db.execute(
        text(
            "SELECT * FROM users WHERE oidc_issuer=:issuer AND oidc_subject=:subject"
        ),
        {"issuer": issuer, "subject": subject},
    ).fetchone()
    if existing:
        if not bool(existing._mapping.get("is_active")):
            raise OIDCIdentityError("user_inactive")
        db.execute(
            text("UPDATE users SET last_login=:now, email=:email WHERE id=:id"),
            {
                "now": datetime.now(timezone.utc).isoformat(),
                "email": email,
                "id": existing.id,
            },
        )
        return dict(existing._mapping)

    legacy = db.execute(
        text(
            "SELECT * FROM users WHERE oidc_issuer IS NULL "
            "AND oidc_subject=:subject AND oidc_provider=:provider ORDER BY id"
        ),
        {"subject": subject, "provider": provider},
    ).fetchall()
    if len(legacy) > 1:
        raise OIDCIdentityError("oidc_identity_ambiguous")
    if len(legacy) == 1:
        row = legacy[0]
        if not bool(row._mapping.get("is_active")):
            raise OIDCIdentityError("user_inactive")
        try:
            changed = db.execute(
                text(
                    "UPDATE users SET oidc_issuer=:issuer, last_login=:now, email=:email "
                    "WHERE id=:id AND oidc_issuer IS NULL"
                ),
                {
                    "issuer": issuer,
                    "now": datetime.now(timezone.utc).isoformat(),
                    "email": email,
                    "id": row.id,
                },
            )
            if changed.rowcount != 1:
                raise OIDCIdentityError("oidc_identity_ambiguous")
        except IntegrityError as exc:
            raise OIDCIdentityError("oidc_identity_ambiguous") from exc
        bound = dict(row._mapping)
        bound["oidc_issuer"] = issuer
        bound["email"] = email
        return bound

    if not config.get("auto_create_users", False):
        raise OIDCIdentityError("user_not_found")
    if config.get("default_role", "viewer") != "viewer":
        raise OIDCIdentityError("oidc_role_configuration_invalid")
    group_id = config.get("default_group_id")
    if group_id is None or not db.execute(
        text("SELECT 1 FROM groups WHERE id=:id AND is_org IS TRUE"),
        {"id": group_id},
    ).fetchone():
        raise OIDCIdentityError("oidc_default_tenant_required")

    username = email.split("@", 1)[0]
    base_username = username
    counter = 1
    while db.execute(
        text("SELECT id FROM users WHERE username=:username"), {"username": username}
    ).fetchone():
        username = f"{base_username}{counter}"
        counter += 1
    try:
        user_id = execute_insert_returning_id(
            db,
            "INSERT INTO users "
            "(username, email, password_hash, role, oidc_subject, oidc_provider, "
            "oidc_issuer, group_id, last_login) VALUES "
            "(:username, :email, '', 'viewer', :subject, :provider, :issuer, :group_id, :now)",
            {
                "username": username,
                "email": email,
                "subject": subject,
                "provider": provider,
                "issuer": issuer,
                "group_id": group_id,
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )
    except IntegrityError as exc:
        raise OIDCIdentityError("oidc_identity_ambiguous") from exc
    return {
        "id": user_id,
        "username": username,
        "email": email,
        "role": "viewer",
        "is_active": True,
        "group_id": group_id,
        "oidc_subject": subject,
        "oidc_provider": provider,
        "oidc_issuer": issuer,
    }


def _record_session(db, user_id, access_token, ip, user_agent):
    """Record an active session from a JWT token."""
    import jwt as _jwt
    try:
        payload = _jwt.decode(access_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
        jti = payload.get("jti")
        if jti:
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
            db.execute(text(f"""{sql.insert_or_ignore_prefix()} active_sessions (user_id, token_jti, ip_address, user_agent)
                               VALUES (:uid, :jti, :ip, :ua){sql.on_conflict_ignore('token_jti')}"""),
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
    row = db.execute(text("SELECT * FROM oidc_config WHERE is_enabled IS TRUE LIMIT 1")).fetchone()
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

    row = db.execute(text("SELECT * FROM oidc_config WHERE is_enabled IS TRUE LIMIT 1")).fetchone()
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
        resolved_user = _resolve_oidc_user(db, config, id_token_claims, user_info)
        user_id = resolved_user["id"]
        user_role = resolved_user["role"]
        username = resolved_user["username"]

        access_token = create_access_token(data={
            "sub": username,
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

    except OIDCIdentityError as e:
        log.warning("OIDC identity resolution failed: %s", e.code)
        db.rollback()
        return RedirectResponse(url=f"/?error={quote(e.code)}", status_code=302)
    except Exception as e:
        log.error(f"OIDC callback error: {e}", exc_info=True)
        db.rollback()
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
async def get_oidc_config(current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Get full OIDC configuration. Superadmin only — system-wide auth config."""
    row = db.execute(text("SELECT * FROM oidc_config LIMIT 1")).fetchone()
    if not row:
        return {"configured": False}
    config = dict(row._mapping)
    if "client_secret_encrypted" in config:
        config["has_client_secret"] = bool(config["client_secret_encrypted"])
        del config["client_secret_encrypted"]
    return config


@router.put("/admin/oidc", tags=["Admin"])
async def update_oidc_config(request: Request, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Update OIDC configuration. Superadmin only."""
    data = await request.json()
    client_secret = data.get("client_secret")
    if client_secret:
        from core.crypto import encrypt
        data["client_secret_encrypted"] = encrypt(client_secret)
        del data["client_secret"]

    allowed_fields = [
        "display_name", "client_id", "client_secret_encrypted", "tenant_id",
        "discovery_url", "scopes", "auto_create_users", "default_role",
        "default_group_id", "is_enabled"
    ]
    proposed_role = data.get("default_role")
    if proposed_role is not None and proposed_role != "viewer":
        raise HTTPException(status_code=422, detail="OIDC auto-provisioning role must be viewer")
    if "default_group_id" in data and data["default_group_id"] is not None:
        if not db.execute(
            text("SELECT 1 FROM groups WHERE id=:id AND is_org IS TRUE"),
            {"id": data["default_group_id"]},
        ).fetchone():
            raise HTTPException(status_code=422, detail="OIDC default tenant is invalid")
    current = db.execute(
        text(
            "SELECT auto_create_users, default_role, default_group_id "
            "FROM oidc_config WHERE id=1"
        )
    ).fetchone()
    effective_auto_create = data.get(
        "auto_create_users", bool(current.auto_create_users) if current else False
    )
    effective_role = data.get(
        "default_role", current.default_role if current else "viewer"
    )
    effective_group = data.get(
        "default_group_id", current.default_group_id if current else None
    )
    if effective_auto_create and (
        effective_role != "viewer" or effective_group is None
    ):
        raise HTTPException(
            status_code=422,
            detail="OIDC auto-provisioning requires viewer role and a default tenant",
        )
    updates = []
    params = {}
    for field in allowed_fields:
        if field in data:
            updates.append(f"{field} = :{field}")
            params[field] = data[field]

    if updates:
        updates.append(f"updated_at = {sql.now()}")
        query = f"UPDATE oidc_config SET {', '.join(updates)} WHERE id = 1"
        db.execute(text(query), params)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        log_audit(db, "oidc_config_updated", "system", details={"fields": list(params.keys())})
        db.commit()
    return {"success": True}
