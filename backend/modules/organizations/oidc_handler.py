"""
O.D.I.N. — OIDC Authentication Handler

Supports Microsoft Entra ID (Azure AD) for enterprise SSO.
GCC High compatible - uses configurable endpoints.

Flow:
1. User clicks "Sign in with Microsoft" 
2. Backend redirects to Microsoft login
3. Microsoft redirects back with auth code
4. Backend exchanges code for tokens
5. Backend creates/updates user, issues JWT
6. Frontend receives JWT and logs in
"""

import os
import json
import logging
import secrets
import httpx
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, quote
from typing import Optional, Dict, Any

log = logging.getLogger("oidc")

# State tokens are stored in SQLite via _state_db_*() helpers below.
# The in-memory dict is kept only as a fast fallback for the brief
# window between startup and the first DB write.


def _state_db_store(state: str, expires: datetime):
    """Persist an OIDC state token to SQLite."""
    try:
        from core.db import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            db.execute(text(
                "INSERT OR REPLACE INTO oidc_pending_states (state, expires_at) VALUES (:s, :e)"
            ), {"s": state, "e": expires.isoformat()})
            db.commit()
        finally:
            db.close()
    except Exception:
        log.warning("Failed to persist OIDC state to DB — falling back to memory")


def _state_db_validate(state: str) -> bool:
    """Check and consume an OIDC state token from SQLite. Returns True if valid."""
    try:
        from core.db import SessionLocal
        from sqlalchemy import text
        db = SessionLocal()
        try:
            row = db.execute(
                text("SELECT expires_at FROM oidc_pending_states WHERE state = :s"),
                {"s": state},
            ).fetchone()
            if not row:
                return False
            # Always delete (consume) the token
            db.execute(text("DELETE FROM oidc_pending_states WHERE state = :s"), {"s": state})
            db.commit()
            exp = datetime.fromisoformat(row[0])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < exp
        finally:
            db.close()
    except Exception:
        log.warning("Failed to validate OIDC state from DB", exc_info=True)
        return False


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
    except Exception:
        pass


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
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        
        # Use custom discovery URL or default based on environment
        if discovery_url:
            self.discovery_url = discovery_url
        else:
            template = self.DISCOVERY_URLS.get(environment, self.DISCOVERY_URLS["commercial"])
            self.discovery_url = template.format(tenant=tenant_id)
        
        self._config: Optional[Dict[str, Any]] = None
        self._config_fetched_at: Optional[datetime] = None
    
    async def _get_oidc_config(self) -> Dict[str, Any]:
        """Fetch OIDC configuration from discovery endpoint. Cached for 1 hour."""
        now = datetime.now(timezone.utc)

        if self._config and self._config_fetched_at:
            age = (now - self._config_fetched_at).total_seconds()
            if age < 3600:  # Cache for 1 hour
                return self._config

        async with httpx.AsyncClient() as client:
            resp = await client.get(self.discovery_url, timeout=10)
            resp.raise_for_status()
            self._config = resp.json()
            self._config_fetched_at = now
            log.info(f"Fetched OIDC config from {self.discovery_url}")
            return self._config
    
    async def get_authorization_url(self, state: Optional[str] = None) -> tuple[str, str]:
        """
        Generate authorization URL for redirect.
        Returns (url, state) tuple.
        """
        config = await self._get_oidc_config()
        auth_endpoint = config["authorization_endpoint"]
        
        # Generate state for CSRF protection
        if not state:
            state = secrets.token_urlsafe(32)

        # Store state with expiry in SQLite
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        _state_db_store(state, expires)

        # Periodic cleanup of expired states
        _state_db_cleanup()
        
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
            "response_mode": "query",
            "prompt": "select_account",  # Always show account picker
        }
        
        url = f"{auth_endpoint}?{urlencode(params)}"
        return url, state
    
    def validate_state(self, state: str) -> bool:
        """Validate and consume state token from callback (DB-backed)."""
        return _state_db_validate(state)
    
    async def exchange_code(self, code: str) -> Dict[str, Any]:
        """
        Exchange authorization code for tokens.
        Returns dict with access_token, id_token, etc.
        """
        config = await self._get_oidc_config()
        token_endpoint = config["token_endpoint"]
        
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                token_endpoint,
                data=data,
                timeout=10,
            )
            
            if resp.status_code != 200:
                log.error(f"Token exchange failed: {resp.status_code} {resp.text}")
                raise Exception(f"Token exchange failed: {resp.text}")
            
            return resp.json()
    
    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """
        Get user info from Microsoft Graph API.
        Returns dict with id, displayName, mail, etc.
        """
        # Microsoft Graph endpoint (same for commercial and GCC High)
        graph_url = "https://graph.microsoft.com/v1.0/me"
        
        # For GCC High, use different endpoint
        if "microsoftonline.us" in self.discovery_url:
            graph_url = "https://graph.microsoft.us/v1.0/me"
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                graph_url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            
            if resp.status_code != 200:
                log.error(f"User info fetch failed: {resp.status_code} {resp.text}")
                raise Exception(f"Failed to get user info: {resp.text}")
            
            return resp.json()
    
    async def parse_id_token(self, id_token: str) -> Dict[str, Any]:
        """
        Parse and validate ID token signature against the provider's JWKS endpoint.
        Uses PyJWT's PyJWKClient for JWKS fetching and key resolution.
        """
        import jwt as _jwt
        from jwt import PyJWKClient

        config = await self._get_oidc_config()
        jwks_uri = config.get("jwks_uri")
        if not jwks_uri:
            raise ValueError("No jwks_uri in OIDC config")

        try:
            jwk_client = PyJWKClient(jwks_uri)
            signing_key = jwk_client.get_signing_key_from_jwt(id_token)

            claims = _jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.client_id,
                options={"verify_exp": True, "verify_aud": True},
            )
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
    )
