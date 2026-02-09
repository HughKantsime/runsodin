"""
OIDC Authentication Handler for PrintFarm Scheduler

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
from datetime import datetime, timedelta
from urllib.parse import urlencode, quote
from typing import Optional, Dict, Any

log = logging.getLogger("oidc")

# State tokens for CSRF protection (in-memory, cleared on restart)
# In production, use Redis or database
_pending_states: Dict[str, datetime] = {}


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
        now = datetime.utcnow()
        
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
        
        # Store state with expiry
        _pending_states[state] = datetime.utcnow() + timedelta(minutes=10)
        
        # Clean old states
        now = datetime.utcnow()
        expired = [s for s, exp in _pending_states.items() if exp < now]
        for s in expired:
            del _pending_states[s]
        
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
        """Validate state token from callback."""
        if state not in _pending_states:
            return False
        
        expiry = _pending_states.pop(state)
        return datetime.utcnow() < expiry
    
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
    
    def parse_id_token(self, id_token: str) -> Dict[str, Any]:
        """
        Parse ID token without validation (we trust Microsoft's signature).
        In production, you'd validate the signature with JWKS.
        """
        import base64
        
        # ID token is JWT: header.payload.signature
        parts = id_token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid ID token format")
        
        # Decode payload (middle part)
        payload = parts[1]
        # Add padding if needed
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)


def create_handler_from_config(config: Dict[str, Any], redirect_uri: str) -> OIDCHandler:
    """Create OIDCHandler from database config row."""
    from crypto import decrypt
    
    client_secret = config.get("client_secret_encrypted")
    if client_secret:
        try:
            client_secret = decrypt(client_secret)
        except:
            pass  # Not encrypted or decryption failed
    
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
