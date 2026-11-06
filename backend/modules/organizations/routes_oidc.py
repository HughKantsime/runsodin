"""Organizations OIDC routes — SSO/OIDC login, callback, token exchange, admin config."""

import hashlib
import hmac
import logging
import os
import secrets
from urllib.parse import urlsplit, parse_qs
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from core.remote_access import session_cookie_secure
from fastapi import APIRouter, Depends, HTTPException, Request
from license_manager import feature_gate
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import execute_insert_returning_id, sql
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, require_superadmin, require_interactive_session
import core.auth as auth_module
from core.auth import create_access_token
from core.config import settings as _settings
from core.rate_limit import limiter

log = logging.getLogger("odin.api")
router = APIRouter()


# Browser binding for the login. The login request sets an HttpOnly cookie whose
# value is a keyed MAC of that login's OAuth `state`, so:
# - the callback only completes in the browser that started *that* login. A
#   victim's browser cannot be made to finish an attacker's IdP flow (login CSRF),
#   because its cookie belongs to a different state;
# - the one-time ODIN code is stored as sha256(binding:code) and the exchange
#   recomputes it from the cookie, so a code read from a log, a URL or the
#   database is useless without that cookie.
OIDC_BINDING_COOKIE = "odin_oidc_bind"
_OIDC_BINDING_MAX_AGE = 600


def oidc_state_binding(state: str) -> str:
    mac = hmac.new(auth_module.SECRET_KEY.encode(), f"odin-oidc-binding:{state}".encode(), hashlib.sha256)
    return mac.hexdigest()


def oidc_code_key(code: str, binding: str) -> str:
    return hashlib.sha256(f"{binding}:{code}".encode()).hexdigest()


def _redirect_uri(request: Request) -> str:
    if _settings.oidc_redirect_uri:
        return _settings.oidc_redirect_uri
    return f"{str(request.base_url).rstrip('/')}/api/auth/oidc/callback"


def _binding_cookie_path(request: Request) -> str:
    # The callback's directory (".../auth/oidc"), which also covers the exchange.
    return urlsplit(_redirect_uri(request)).path.rsplit("/", 1)[0] or "/"


def _binding_cookie_paths(request: Request) -> list[str]:
    """One path covering the /api and /api/v1 callback and exchange (the web client exchanges
    on /api/auth/oidc/exchange): the API base of OIDC_REDIRECT_URI's directory, "/api"
    normally or e.g. "/odin/api" behind a path-prefix proxy. One cookie, so clients that look
    cookies up by name see one value."""
    directory = _binding_cookie_path(request)
    for suffix in ("/v1/auth/oidc", "/auth/oidc"):
        if directory.endswith(suffix):
            return [directory[: -len(suffix)] or "/"]
    return ["/api"]


def _binding_cookie_secure(request: Request) -> bool:
    """Verified HTTPS is always Secure; HTTP retains the existing override policy."""
    if request.url.scheme == "https":
        return True
    if os.environ.get("COOKIE_SECURE") is not None:
        return bool(_settings.cookie_secure)
    return request.url.scheme == "https"


def _delete_binding_cookies(response, request: Request) -> None:
    for path in _binding_cookie_paths(request):
        response.delete_cookie(OIDC_BINDING_COOKIE, path=path)


class OIDCIdentityError(RuntimeError):
    def __init__(self, code: str, username=None):
        super().__init__(code)
        self.code = code
        self.username = username


def _provider_identity(config: dict, claims: dict, user_info: dict) -> tuple[str, bool]:
    """Validate provider policy and return a normalized email plus verification."""
    provider_type = str(config.get("provider_type") or "microsoft").lower()
    issuer = claims.get("iss")
    if user_info.get("sub") and user_info.get("sub") != claims.get("sub"):
        raise OIDCIdentityError("userinfo_subject_mismatch")

    email = (
        claims.get("email")
        or user_info.get("email")
        or user_info.get("mail")
        or user_info.get("userPrincipalName")
        or user_info.get("preferred_username")
        or claims.get("preferred_username")
    )
    if not isinstance(email, str) or not email.strip() or "@" not in email:
        raise OIDCIdentityError("missing_email")
    email = email.strip().lower()

    verified = claims.get("email_verified") is True or user_info.get("email_verified") is True
    if provider_type == "google":
        if issuer not in {"https://accounts.google.com", "accounts.google.com"}:
            raise OIDCIdentityError("google_issuer_invalid")
        if not verified:
            raise OIDCIdentityError("google_email_unverified")
        domains = {
            part.strip().lower()
            for part in str(config.get("allowed_domains") or "").split(",")
            if part.strip()
        }
        if domains:
            hosted_domain = claims.get("hd")
            if not isinstance(hosted_domain, str) or hosted_domain.lower() not in domains:
                raise OIDCIdentityError("google_hosted_domain_invalid")
            if email.rsplit("@", 1)[1] not in domains:
                raise OIDCIdentityError("google_email_domain_invalid")
    return email, verified


def _resolve_oidc_user(
    db: Session,
    config: dict,
    claims: dict,
    user_info: dict,
    *,
    verified_email: bool = False,
) -> dict:
    """Resolve, bind, or create one exact issuer+subject identity."""
    issuer = claims.get("iss")
    subject = claims.get("sub")
    email = (
        claims.get("email")
        or user_info.get("email")
        or user_info.get("mail")
        or user_info.get("userPrincipalName")
        or user_info.get("preferred_username")
        or claims.get("preferred_username")
    )
    if not isinstance(issuer, str) or not issuer:
        raise OIDCIdentityError("missing_issuer")
    if not isinstance(subject, str) or not subject:
        raise OIDCIdentityError("missing_subject")
    if not isinstance(email, str) or not email.strip():
        raise OIDCIdentityError("missing_email")
    email = email.strip().lower()

    provider = config.get("display_name", "oidc").lower().replace(" ", "_")
    existing = db.execute(
        text(
            "SELECT * FROM users WHERE oidc_issuer=:issuer AND oidc_subject=:subject"
        ),
        {"issuer": issuer, "subject": subject},
    ).fetchone()
    if existing:
        if not bool(existing._mapping.get("is_active")):
            raise OIDCIdentityError("user_inactive", existing.username)
        db.execute(
            text("UPDATE users SET last_login=:now, email=:email WHERE id=:id"),
            {
                "now": datetime.now(timezone.utc).isoformat(),
                "email": email,
                "id": existing.id,
            },
        )
        resolved = dict(existing._mapping)
        resolved["email"] = email
        return resolved

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
            raise OIDCIdentityError("user_inactive", row.username)
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

    group_id = config.get("default_group_id")
    valid_group = group_id is not None and db.execute(
        text("SELECT 1 FROM groups WHERE id=:id AND is_org IS TRUE"),
        {"id": group_id},
    ).fetchone()

    if verified_email:
        if not valid_group:
            raise OIDCIdentityError("oidc_default_tenant_required")
        matches = db.execute(
            text("SELECT * FROM users WHERE LOWER(email)=:email ORDER BY id"),
            {"email": email},
        ).fetchall()
        if matches:
            if len(matches) != 1:
                raise OIDCIdentityError("oidc_email_ambiguous")
            row = matches[0]
            mapping = row._mapping
            # A verified IdP email may adopt an unbound, passwordless, MFA-free
            # placeholder account in the same tenant. It must never silently take
            # over an account that already holds its own credentials (a local
            # password or MFA): that account's owner authenticates directly, and
            # auto-link would hand it to whoever controls the matching IdP email.
            has_local_password = bool((mapping.get("password_hash") or "").strip())
            has_mfa = bool(mapping.get("mfa_enabled"))
            if (
                mapping.get("group_id") != group_id
                or not bool(mapping.get("is_active"))
                or mapping.get("oidc_issuer") is not None
                or mapping.get("oidc_subject") is not None
                or has_local_password
                or has_mfa
            ):
                raise OIDCIdentityError("oidc_email_conflict")
            try:
                changed = db.execute(
                    text(
                        "UPDATE users SET oidc_issuer=:issuer, oidc_subject=:subject, "
                        "oidc_provider=:provider, last_login=:now "
                        "WHERE id=:id AND oidc_issuer IS NULL AND oidc_subject IS NULL"
                    ),
                    {
                        "issuer": issuer,
                        "subject": subject,
                        "provider": provider,
                        "now": datetime.now(timezone.utc).isoformat(),
                        "id": row.id,
                    },
                )
                if changed.rowcount != 1:
                    raise OIDCIdentityError("oidc_email_conflict")
            except IntegrityError as exc:
                raise OIDCIdentityError("oidc_email_conflict") from exc
            resolved = dict(mapping)
            resolved.update(
                oidc_issuer=issuer,
                oidc_subject=subject,
                oidc_provider=provider,
                email=email,
            )
            return resolved

    if not config.get("auto_create_users", False):
        raise OIDCIdentityError("user_not_found")
    if config.get("default_role", "viewer") != "viewer":
        raise OIDCIdentityError("oidc_role_configuration_invalid")
    if not valid_group:
        raise OIDCIdentityError("oidc_default_tenant_required")

    # Auto-provisioning creates a user like POST /api/users does, so it is
    # bound by the same license user cap (Community: 1 user).
    from license_manager import check_user_limit
    try:
        check_user_limit(db.execute(text("SELECT COUNT(*) FROM users")).scalar())
    except HTTPException as exc:
        raise OIDCIdentityError("license_user_limit_reached") from exc

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
        "_created": True,
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


from core.session_tracking import stage_session as _stage_session


# ============== OIDC public config ==============

@router.get("/auth/oidc/config", tags=["Auth"])
async def get_oidc_public_config(db: Session = Depends(get_db)):
    """Get public OIDC config for login page (is SSO enabled, display name)."""
    row = db.execute(text("SELECT is_enabled, display_name FROM oidc_config LIMIT 1")).fetchone()
    if not row:
        return {"enabled": False}
    return {"enabled": bool(row[0]), "display_name": row[1] or "Single Sign-On"}


# ============== OIDC login flow ==============

# TrustedProxySchemeMiddleware resolves request.client using ODIN_TRUSTED_PROXIES
# before slowapi reads it; never interpret forwarding headers again here.
@router.get("/auth/oidc/login", tags=["Auth"])
@limiter.shared_limit("30/minute", scope="oidc-login")
async def oidc_login(request: Request, db: Session = Depends(get_db)):
    """Initiate OIDC login flow. Redirects to identity provider."""
    from modules.organizations.oidc_handler import create_handler_from_config
    row = db.execute(text("SELECT * FROM oidc_config WHERE is_enabled IS TRUE LIMIT 1")).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="OIDC not configured")
    config = dict(row._mapping)
    handler = create_handler_from_config(config, _redirect_uri(request))
    url, state = await handler.get_authorization_url()
    from fastapi.responses import RedirectResponse
    response = RedirectResponse(url=url, status_code=302)
    # SameSite=Lax so the IdP's top-level redirect back to the callback carries it.
    binding, secure = oidc_state_binding(state), _binding_cookie_secure(request)
    for path in _binding_cookie_paths(request):
        response.set_cookie(key=OIDC_BINDING_COOKIE, value=binding, httponly=True, secure=secure,
                            samesite="lax", path=path, max_age=_OIDC_BINDING_MAX_AGE)
    return response


# ============== OIDC callback ==============

async def _oidc_callback(request: Request, code: str = None, state: str = None,
                        error: str = None, error_description: str = None, db: Session = Depends(get_db)):
    """Handle OIDC callback from identity provider."""
    from modules.organizations.oidc_handler import create_handler_from_config
    from fastapi.responses import RedirectResponse

    if error:
        log.warning("OIDC provider refused sign-in")
        return RedirectResponse(url=f"/?error={quote(str(error))}", status_code=302)
    if not code or not state:
        return RedirectResponse(url="/?error=missing_params", status_code=302)

    row = db.execute(text("SELECT * FROM oidc_config WHERE is_enabled IS TRUE LIMIT 1")).fetchone()
    if not row:
        return RedirectResponse(url="/?error=oidc_not_configured", status_code=302)
    config = dict(row._mapping)

    # Only the browser that started *this* login may complete it (login CSRF).
    binding = request.cookies.get(OIDC_BINDING_COOKIE) or ""
    if not hmac.compare_digest(binding, oidc_state_binding(state)):
        return RedirectResponse(url="/?error=invalid_state", status_code=302)

    handler = create_handler_from_config(config, _redirect_uri(request))
    if not handler.validate_state(state):
        return RedirectResponse(url="/?error=invalid_state", status_code=302)

    try:
        tokens = await handler.exchange_code(code)
        id_token_claims = await handler.parse_id_token(tokens["id_token"])
        user_info = await handler.get_user_info(tokens["access_token"])
        email, verified_email = _provider_identity(config, id_token_claims, user_info)
        id_token_claims = {**id_token_claims, "email": email}
        resolved_user = _resolve_oidc_user(
            db,
            config,
            id_token_claims,
            user_info,
            verified_email=verified_email,
        )
        request.state.oidc_audit_username = resolved_user["username"]
        user_id = resolved_user["id"]
        if resolved_user.get("_created"):
            log_audit(db, "user.created", "user", user_id, {"role": "viewer"})
        user_role = resolved_user["role"]
        username = resolved_user["username"]

        access_token = create_access_token(data={
            "sub": username,
            "role": user_role,
        })

        from core.crypto import encrypt as _crypto_encrypt
        oidc_code = secrets.token_urlsafe(48)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        db.execute(text("INSERT INTO oidc_auth_codes (code, access_token, expires_at) VALUES (:code, :token, :exp)"),
                   {"code": oidc_code_key(oidc_code, binding),
                    "token": _crypto_encrypt(access_token), "exp": expires_at})
        db.commit()
        # Fragment, not query: browsers never send it to any server, so it can't
        # reach access logs, proxies or Referer headers.
        return RedirectResponse(url=f"/#oidc_code={oidc_code}", status_code=302)

    except OIDCIdentityError as e:
        if e.username is not None: request.state.oidc_audit_username = e.username
        log.warning("OIDC identity resolution failed: %s", e.code)
        db.rollback()
        return RedirectResponse(url=f"/?error={quote(e.code)}", status_code=302)
    except Exception as e:
        log.error("OIDC callback failed")
        db.rollback()
        return RedirectResponse(url=f"/?error=auth_failed", status_code=302)


# ============== OIDC code exchange ==============

async def _oidc_exchange_code(body: dict, request: Request, db: Session = Depends(get_db), *, audit_method: str = "oidc"):
    """Exchange a one-time OIDC auth code for a JWT access token."""
    code = body.get("code", "")
    if not code or not isinstance(code, str):
        raise HTTPException(status_code=400, detail="code is required")
    binding = request.cookies.get(OIDC_BINDING_COOKIE) or ""
    if len(binding) != 64:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    code = oidc_code_key(code, binding)

    try:
        row = db.execute(text("SELECT access_token, expires_at FROM oidc_auth_codes WHERE code = :code"),
                         {"code": code}).fetchone()
        consumed = 0
        if row:
            # Only the exact payload observed by this request can be consumed.
            deletion = db.execute(text(
                "DELETE FROM oidc_auth_codes WHERE code=:code "
                "AND access_token=:token AND expires_at=:expiry"),
                {"code": code, "token": row.access_token, "expiry": row.expires_at})
            consumed = deletion.rowcount
        db.execute(text("DELETE FROM oidc_auth_codes WHERE expires_at < :now"),
                   {"now": datetime.now(timezone.utc).isoformat()})
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="Login temporarily unavailable. Try again later.") from None

    if not row or consumed != 1:
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
        payload = _jwt.decode(access_token, auth_module.SECRET_KEY,
                              algorithms=[auth_module.ALGORITHM],
                              options={"require": ["exp", "sub", "jti"]})
        username = payload["sub"]
        if (not isinstance(username, str) or not username
                or not isinstance(payload["jti"], str) or not payload["jti"]
                or "mfa_pending" in payload or "mfa_setup_required" in payload):
            raise ValueError("Invalid interactive identity")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid authentication session") from None
    request.state.oidc_audit_username = username
    try:
        revoked = db.execute(
            text("SELECT 1 FROM token_blacklist WHERE jti = :jti"),
            {"jti": payload["jti"]},
        ).fetchone()
    except Exception:
        raise HTTPException(status_code=503, detail="Login temporarily unavailable. Try again later.") from None
    if revoked:
        raise HTTPException(status_code=401, detail="Invalid authentication session")
    try:
        user = db.execute(text("SELECT id,is_active FROM users WHERE username=:u"),
                          {"u": username}).fetchone()
    except Exception:
        raise HTTPException(status_code=503, detail="Login temporarily unavailable. Try again later.") from None
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid authentication session")
    from core.mfa_login import login_mfa_response
    continuation = login_mfa_response(db, user.id)
    if continuation is not None:
        from fastapi.responses import JSONResponse
        pending = JSONResponse(continuation)
        _delete_binding_cookies(pending, request)
        return pending
    client_ip = request.client.host if request.client else "unknown"
    if _stage_session(db, user.id, access_token, client_ip, request.headers.get("user-agent", "")) is not True:
        raise HTTPException(status_code=503, detail="Login temporarily unavailable. Try again later.")

    from modules.organizations.sso_audit import prepare_success
    prepare_success(db, user.id)
    log_audit(db, "auth.login", "user", user.id, {"method": audit_method, "username": username})
    db.commit()

    from fastapi.responses import JSONResponse
    resp = JSONResponse({"access_token": access_token, "token_type": "bearer"})
    _delete_binding_cookies(resp, request)
    resp.set_cookie(key="session", value=access_token, httponly=True,
                    secure=session_cookie_secure(request, _settings), samesite=_settings.cookie_samesite,
                    path="/", max_age=86400)
    return resp



# The provider's error parameter is untrusted free text. Retain only recognized
# protocol/internal codes; descriptions and arbitrary values never reach details.
_OIDC_FAILURE_CODES = frozenset({
    'access_denied', 'account_selection_required', 'auth_failed', 'consent_required',
    'google_email_domain_invalid', 'google_email_unverified', 'google_hosted_domain_invalid',
    'google_issuer_invalid', 'interaction_required', 'invalid_request', 'invalid_scope',
    'invalid_state', 'license_user_limit_reached', 'login_required', 'missing_email',
    'missing_issuer', 'missing_params', 'missing_subject', 'oidc_default_tenant_required',
    'oidc_email_ambiguous', 'oidc_email_conflict', 'oidc_identity_ambiguous',
    'oidc_not_configured', 'oidc_role_configuration_invalid', 'server_error',
    'temporarily_unavailable', 'unauthorized_client', 'unsupported_response_type', 'user_inactive',
    'user_not_found', 'userinfo_subject_mismatch',
})
_EXCHANGE_FAILURE_CODES = {
    "code is required": "missing_params",
    "Invalid or expired code": "invalid_code",
    "Code expired": "code_expired",
    "Invalid authentication session": "invalid_session",
    "Login temporarily unavailable. Try again later.": "auth_unavailable",
}


from modules.organizations.sso_audit import failure as _audit_oidc_failure


@router.get("/auth/oidc/callback", tags=["Auth"])
async def oidc_callback(request: Request, code: str = None, state: str = None,
                        error: str = None, error_description: str = None, db: Session = Depends(get_db)):
    try:
        response = await _oidc_callback(request, code, state, error, error_description, db)
    except Exception:
        _audit_oidc_failure(db, "auth_failed", request)
        raise HTTPException(503, "Login temporarily unavailable. Try again later.") from None
    reason = parse_qs(urlsplit(response.headers.get("location", "")).query).get("error", [None])[0]
    if reason is not None:
        _audit_oidc_failure(db, reason if reason in _OIDC_FAILURE_CODES else "auth_failed", request)
    return response


@router.post("/auth/oidc/exchange", tags=["Auth"])
async def oidc_exchange_code(body: dict, request: Request, db: Session = Depends(get_db)):
    try:
        return await _oidc_exchange_code(body, request, db)
    except HTTPException as exc:
        _audit_oidc_failure(db, _EXCHANGE_FAILURE_CODES.get(str(exc.detail), "auth_failed"), request)
        raise
    except Exception:
        _audit_oidc_failure(db, "auth_failed", request)
        raise HTTPException(503, "Login temporarily unavailable. Try again later.") from None


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


# Single sign-on is a Pro feature (license_manager.TIERS "sso"): configuring it
# needs Pro; reading the stored configuration stays available after a downgrade.
@router.put("/admin/oidc", tags=["Admin"], dependencies=[Depends(require_interactive_session()), Depends(feature_gate("sso"))])
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
        "default_group_id", "provider_type", "allowed_domains", "is_enabled"
    ]
    provider_type = data.get("provider_type")
    if provider_type is not None and provider_type not in {"microsoft", "google", "generic"}:
        raise HTTPException(status_code=422, detail="OIDC provider type is invalid")
    if "allowed_domains" in data:
        domains = [part.strip().lower() for part in str(data["allowed_domains"] or "").split(",") if part.strip()]
        if any("@" in domain or "." not in domain for domain in domains):
            raise HTTPException(status_code=422, detail="OIDC allowed domains are invalid")
        data["allowed_domains"] = ",".join(dict.fromkeys(domains))
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
        # Fresh installs have no config row. Initialize without replacing an
        # existing row (which would erase the saved secret and omitted fields).
        # ON CONFLICT is supported by both SQLite and PostgreSQL.
        db.execute(text("INSERT INTO oidc_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))
        updates.append(f"updated_at = {sql.now()}")
        query = f"UPDATE oidc_config SET {', '.join(updates)} WHERE id = 1"
        db.execute(text(query), params)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        log_audit(db, "oidc_config_updated", "system", details={"fields": list(params.keys())})
        db.commit()
    return {"success": True}
