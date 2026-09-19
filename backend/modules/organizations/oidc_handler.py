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
import httpx
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from typing import Optional, Dict, Any

log = logging.getLogger("oidc")

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
        if discovery_url:
            self.discovery_url = discovery_url
        elif provider_type == "google":
            self.discovery_url = "https://accounts.google.com/.well-known/openid-configuration"
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

        # v1.8.9 (codex pass 11): ITAR guard with DNS pinning. The
        # bare check was TOCTOU — resolution could drift between
        # check and httpx connect. pin_for_request pins the socket
        # to the vetted addresses for the duration of the block.
        from core.itar import pin_for_request, should_trust_env
        with pin_for_request(self.discovery_url):
            # trust_env=should_trust_env() (codex pass 12): httpx honors
            # HTTP(S)_PROXY by default, which would route the socket
            # through a proxy and bypass our DNS pin entirely. The
            # proxy then does its own lookup and can connect to a
            # public IdP. Disable env proxies so the pin holds.
            async with httpx.AsyncClient(trust_env=should_trust_env()) as client:
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
        config = await self._get_oidc_config()
        token_endpoint = config["token_endpoint"]
        
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        
        # v1.8.9 (codex pass 11): ITAR DNS-pinned exchange.
        from core.itar import pin_for_request, should_trust_env
        with pin_for_request(token_endpoint):
            async with httpx.AsyncClient(trust_env=should_trust_env()) as client:
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
        """Fetch allowlisted standard identity claims from discovery userinfo."""
        config = await self._get_oidc_config()
        userinfo_url = config.get("userinfo_endpoint")
        if not isinstance(userinfo_url, str) or not userinfo_url:
            raise ValueError("OIDC discovery document has no userinfo_endpoint")

        # v1.8.9 (codex pass 11): ITAR DNS-pinned Graph fetch.
        from core.itar import pin_for_request, should_trust_env
        with pin_for_request(userinfo_url):
            async with httpx.AsyncClient(trust_env=should_trust_env()) as client:
                resp = await client.get(
                    userinfo_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10,
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
        Fetches JWKS via direct httpx (trust_env=should_trust_env()) so
        HTTP(S)_PROXY env vars cannot defeat ITAR DNS pinning.
        """
        import jwt as _jwt

        config = await self._get_oidc_config()
        jwks_uri = config.get("jwks_uri")
        if not jwks_uri:
            raise ValueError("No jwks_uri in OIDC config")
        issuer = config.get("issuer")
        if not isinstance(issuer, str) or not issuer:
            raise ValueError("No issuer in OIDC config")

        # v1.8.9 (codex pass 11 + 12): ITAR DNS-pinned JWKS fetch.
        # PyJWKClient uses urllib.request internally, which honors
        # HTTP(S)_PROXY env vars — a proxy would defeat the DNS pin.
        # Fetch the JWKS ourselves via httpx with trust_env=should_trust_env(),
        # then build a PyJWKSet from the JSON.
        from core.itar import pin_for_request, should_trust_env
        from jwt import PyJWKSet

        try:
            with pin_for_request(jwks_uri):
                async with httpx.AsyncClient(trust_env=should_trust_env()) as _client:
                    _resp = await _client.get(jwks_uri, timeout=10)
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
