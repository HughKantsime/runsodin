"""
O.D.I.N. — provider-neutral OIDC authentication handler.

Supports standards-based OIDC providers plus explicit Microsoft and Google
policy profiles. Microsoft GCC High remains compatible through configurable
discovery endpoints.

Flow:
1. User clicks "Sign in with Microsoft" 
2. Backend redirects to Microsoft login
3. Microsoft redirects back with auth code
4. Backend exchanges code for tokens
5. Backend creates/updates user, issues JWT
6. Frontend receives JWT and logs in
"""

import logging
import secrets
from collections import OrderedDict
from threading import Lock
from time import monotonic as _cache_clock
from core import itar
from core.outbound_http import pinned_async_request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from typing import Optional, Dict, Any

log = logging.getLogger("oidc")

# Routes create a handler per login. Cache by discovery URL across handlers,
# with a one-hour TTL and a fixed bound on retained admin configuration URLs.
_discovery_cache = OrderedDict()
_discovery_cache_lock = Lock()
_DISCOVERY_CACHE_SIZE = 128

# State tokens are stored in SQLite via _state_db_*() helpers below.
# The in-memory dict is kept only as a fast fallback for the brief
# window between startup and the first DB write.


def _state_db_store(state: str, nonce: str, expires: datetime):
    """Persist an OIDC state token to the database."""
    try:
        from core.db import SessionLocal
        from core.db_compat import sql
        from sqlalchemy import text
        db = SessionLocal()
        try:
            db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                f"{sql.upsert_prefix()} oidc_pending_states (state, nonce, expires_at) "
                f"VALUES (:s, :n, :e){sql.on_conflict_suffix('state', ['nonce', 'expires_at'])}"
            ), {"s": state, "n": nonce, "e": expires.isoformat()})
            db.commit()
        finally:
            db.close()
    except Exception:
        log.warning("Failed to persist OIDC state to DB — falling back to memory")


def _state_db_consume(state: str) -> Optional[str]:
    """Consume a state token and return its expected nonce when still valid."""
    try:
        from core.db import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            row = db.execute(
                text("SELECT expires_at, nonce FROM oidc_pending_states WHERE state = :s"),
                {"s": state},
            ).fetchone()
            if not row:
                return None
            # Always delete (consume) the token
            db.execute(text("DELETE FROM oidc_pending_states WHERE state = :s"), {"s": state})
            db.commit()
            exp = datetime.fromisoformat(row[0])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= exp:
                return None
            nonce = row[1]
            return nonce if isinstance(nonce, str) and nonce else None
        finally:
            db.close()
    except Exception:
        log.warning("Failed to validate OIDC state from DB", exc_info=True)
        return None


def _state_db_cleanup():
    """Remove expired OIDC state tokens from SQLite."""
    try:
        from core.db import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            db.execute(
                text("DELETE FROM oidc_pending_states WHERE expires_at < :now"),
                {"now": datetime.now(timezone.utc).isoformat()},
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        log.debug(f"Failed to clean expired OIDC states: {e}")


def effective_discovery_url(discovery_url, provider_type, tenant_id, environment="commercial"):
    """The discovery address a sign-in will contact: the stored one, else the provider preset's.

    Shared with the ITAR boot audit so both judge the same address.
    """
    if discovery_url:
        return discovery_url
    if provider_type == "google":
        return "https://accounts.google.com/.well-known/openid-configuration"
    template = OIDCHandler.DISCOVERY_URLS.get(environment, OIDCHandler.DISCOVERY_URLS["commercial"])
    return template.format(tenant=tenant_id or "")


class OIDCHandler:
    """
    Handles OIDC authentication with Microsoft Entra ID.
    
    Supports both commercial Azure AD and GCC High endpoints.
    """
    
    # Well-known discovery endpoints
    DISCOVERY_URLS = {
        "commercial": "https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration",
        "gcc_high": "https://login.microsoftonline.us/{tenant}/v2.0/.well-known/openid-configuration",
    }
    
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tenant_id: str,
        redirect_uri: str,
        scopes: str = "openid profile email",
        discovery_url: Optional[str] = None,
        environment: str = "commercial",
        provider_type: str = "microsoft",
        allowed_domains: Optional[str] = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.provider_type = provider_type
        self.allowed_domains = allowed_domains or ""
        self._expected_nonce: Optional[str] = None
        
        # Use custom discovery URL or default based on environment
        self.discovery_url = effective_discovery_url(discovery_url, provider_type, tenant_id, environment)
        
    async def _get_oidc_config(self) -> Dict[str, Any]:
        """Fetch OIDC configuration from discovery endpoint. Cached for 1 hour.

        Under ITAR mode with ODIN_ITAR_ALLOWED_IDP_HOSTS set, a discovery
        document on an allowed host must keep its token, userinfo and JWKS
        endpoints on allowed hosts. The check runs on cached and fresh
        documents, and a refused document is never cached.
        """
        cached_config = None
        with _discovery_cache_lock:
            cached = _discovery_cache.get(self.discovery_url)
            if cached and _cache_clock() < cached[0]:
                _discovery_cache.move_to_end(self.discovery_url)
                cached_config = cached[1]
        if cached_config is not None:
            itar.enforce_idp_endpoint_hosts(self.discovery_url, cached_config)
            return cached_config

        with itar.oidc_request_scope("discovery"):
            resp = await pinned_async_request("GET", self.discovery_url, timeout=10)
        resp.raise_for_status()
        config = resp.json()
        if not isinstance(config, dict):
            raise ValueError("OIDC discovery response is invalid")
        itar.enforce_idp_endpoint_hosts(self.discovery_url, config)
        with _discovery_cache_lock:
            _discovery_cache[self.discovery_url] = (_cache_clock() + 3600, config)
            _discovery_cache.move_to_end(self.discovery_url)
            while len(_discovery_cache) > _DISCOVERY_CACHE_SIZE:
                _discovery_cache.popitem(last=False)
        log.info(f"Fetched OIDC config from {self.discovery_url}")
        return config
    
    async def get_authorization_url(self, state: Optional[str] = None) -> tuple[str, str]:
        """
        Generate authorization URL for redirect.
        Returns (url, state) tuple.
        """
        from license_manager import require_feature
        require_feature("sso")
        config = await self._get_oidc_config()
        auth_endpoint = config["authorization_endpoint"]
        
        # Generate state for CSRF protection
        if not state:
            state = secrets.token_urlsafe(32)

        nonce = secrets.token_urlsafe(32)

        # Store state and its one-time nonce with the same expiry.
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        _state_db_store(state, nonce, expires)

        # Periodic cleanup of expired states
        _state_db_cleanup()
        
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
            "response_mode": "query",
            "prompt": "select_account",
        }
        if self.provider_type == "google":
            domains = [part.strip().lower() for part in self.allowed_domains.split(",") if part.strip()]
            if domains:
                # Google accepts one hd hint. Enforcement uses the signed claim.
                params["hd"] = domains[0]
        
        url = f"{auth_endpoint}?{urlencode(params)}"
        return url, state
    
    def validate_state(self, state: str) -> bool:
        """Validate/consume state and retain the nonce for ID-token validation."""
        self._expected_nonce = _state_db_consume(state)
        return self._expected_nonce is not None
    
    async def exchange_code(self, code: str) -> Dict[str, Any]:
        """
        Exchange authorization code for tokens.
        Returns dict with access_token, id_token, etc.
        """
        from license_manager import require_feature
        require_feature("sso")
        config = await self._get_oidc_config()
        token_endpoint = config["token_endpoint"]
        
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        
        with itar.oidc_request_scope("token"):
            resp = await pinned_async_request("POST", token_endpoint, data=data, timeout=10)
        if resp.status_code != 200:
            log.error(f"Token exchange failed: {resp.status_code} {resp.text}")
            raise Exception(f"Token exchange failed: {resp.text}")
        return resp.json()

    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """Fetch allowlisted standard identity claims from discovery userinfo."""
        from license_manager import require_feature
        require_feature("sso")
        config = await self._get_oidc_config()
        userinfo_url = config.get("userinfo_endpoint")
        if not isinstance(userinfo_url, str) or not userinfo_url:
            raise ValueError("OIDC discovery document has no userinfo_endpoint")

        with itar.oidc_request_scope("userinfo"):
            resp = await pinned_async_request(
                "GET", userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"}, timeout=10,
            )
        if resp.status_code != 200:
            log.error("OIDC userinfo fetch failed with status %s", resp.status_code)
            raise ValueError("OIDC userinfo request failed")

        raw = resp.json()
        if not isinstance(raw, dict):
            raise ValueError("OIDC userinfo response is invalid")
        allowed = {
            "sub", "email", "email_verified", "name", "given_name",
            "family_name", "preferred_username", "mail", "userPrincipalName",
        }
        return {key: raw[key] for key in allowed if key in raw}
    
    async def parse_id_token(self, id_token: str) -> Dict[str, Any]:
        """
        Parse and validate ID token signature against the provider's JWKS endpoint.
        Fetches JWKS through the shared worker so DNS pinning and the
        ITAR proxy policy also cover the signing-key request.
        """
        import jwt as _jwt

        from license_manager import require_feature
        require_feature("sso")
        config = await self._get_oidc_config()
        jwks_uri = config.get("jwks_uri")
        if not jwks_uri:
            raise ValueError("No jwks_uri in OIDC config")
        issuer = config.get("issuer")
        if not isinstance(issuer, str) or not issuer:
            raise ValueError("No issuer in OIDC config")

        # Fetch ourselves: PyJWKClient's proxy handling could bypass the pin.
        from jwt import PyJWKSet

        try:
            with itar.oidc_request_scope("jwks"):
                _resp = await pinned_async_request("GET", jwks_uri, timeout=10)
            _resp.raise_for_status()
            _jwks_json = _resp.json()

            jwk_set = PyJWKSet.from_dict(_jwks_json)
            unverified = _jwt.get_unverified_header(id_token)
            kid = unverified.get("kid")
            signing_key = None
            for k in jwk_set.keys:
                if getattr(k, "key_id", None) == kid:
                    signing_key = k
                    break
            if signing_key is None:
                raise ValueError(f"No JWKS key matches kid={kid!r}")

            allowed_issuers: str | tuple[str, ...] = issuer
            if self.provider_type == "google":
                allowed_issuers = ("https://accounts.google.com", "accounts.google.com")
            claims = _jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=allowed_issuers,
                options={
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "require": ["exp", "aud", "iss", "sub"],
                },
            )
            if not isinstance(claims.get("sub"), str) or not claims["sub"]:
                raise ValueError("ID token subject is missing")
            if not self._expected_nonce or not secrets.compare_digest(
                str(claims.get("nonce", "")), self._expected_nonce
            ):
                raise ValueError("ID token nonce mismatch")
            self._expected_nonce = None
            return claims

        except Exception as e:
            log.error(f"ID token signature validation failed: {e}", exc_info=True)
            raise ValueError(f"ID token validation failed: {e}")


def create_handler_from_config(config: Dict[str, Any], redirect_uri: str) -> OIDCHandler:
    """Create OIDCHandler from database config row."""
    from core.crypto import decrypt
    
    client_secret = config.get("client_secret_encrypted")
    if client_secret:
        try:
            client_secret = decrypt(client_secret)
        except Exception:
            log.warning("Failed to decrypt OIDC client secret — may be stored unencrypted")
    
    # Determine environment from tenant or discovery URL
    environment = "commercial"
    discovery_url = config.get("discovery_url")
    if discovery_url and "microsoftonline.us" in discovery_url:
        environment = "gcc_high"
    
    return OIDCHandler(
        client_id=config.get("client_id", ""),
        client_secret=client_secret or "",
        tenant_id=config.get("tenant_id", ""),
        redirect_uri=redirect_uri,
        scopes=config.get("scopes", "openid profile email"),
        discovery_url=discovery_url,
        environment=environment,
        provider_type=config.get("provider_type", "microsoft"),
        allowed_domains=config.get("allowed_domains"),
    )
