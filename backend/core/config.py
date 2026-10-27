"""
O.D.I.N. — Configuration settings.

Loads from environment variables with sensible defaults.

Copied to core/ as part of the modular architecture refactor.
Old import path (from config import settings) continues to work via re-exports in config.py.
"""

import json
from typing import Annotated, Dict, Optional, List
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    # Database
    database_url: str = "sqlite:///./odin.db"

    # Spoolman integration (optional)
    spoolman_url: Optional[str] = None

    # Optional read-only display source. Empty by default.
    filament_ledger_url: Optional[str] = None
    filament_ledger_printers: Annotated[Dict[int, str], NoDecode] = Field(default_factory=dict)

    @field_validator("filament_ledger_printers", mode="before")
    @classmethod
    def parse_filament_ledger_printers(cls, value):
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else {}
        return {} if value is None else value

    # Scheduler defaults
    blackout_start: str = "22:30"
    blackout_end: str = "05:30"
    scheduler_horizon_days: int = 7

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # Security - leave empty to disable auth (trusted network mode)
    api_key: Optional[str] = None

    # JWT secret for token signing
    jwt_secret_key: Optional[str] = None

    # Encryption key for stored secrets (API keys, etc.)
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: Optional[str] = None

    # Frontend - comma-separated list in .env, e.g. CORS_ORIGINS=http://localhost:3000,http://example.com
    # Default is empty (no cross-origin). Set via CORS_ORIGINS env var in dev/prod.
    cors_origins: str = ""

    # Comma-separated Host header allowlist. Keep "*" for backwards-compatible
    # general self-hosting; EDU deployments should set their exact hostname(s).
    trusted_hosts: str = "*"

    # Optional HTTPS origin; never inferred from an untrusted Host header.
    odin_public_url: str = ""
    odin_tailnet_ip: str = ""
    # Explicit socket peer IPs or DNS names (refreshed every five seconds).
    odin_trusted_proxies: str = "127.0.0.1,::1"

    @field_validator("odin_public_url")
    @classmethod
    def validate_public_url(cls, value):
        from core.remote_access import validate_public_origin
        return validate_public_origin(value)

    @field_validator("odin_tailnet_ip")
    @classmethod
    def validate_tailnet_ip(cls, value):
        from core.remote_access import validate_tailnet_ip
        return validate_tailnet_ip(value)

    # Cookie settings for session auth
    # Set COOKIE_SECURE=false for local HTTP dev (default true for production)
    cookie_secure: bool = True
    # SameSite policy: 'strict' (prod), 'lax' (needed if OIDC IdP is cross-origin)
    cookie_samesite: str = "strict"

    # License server URL for online activation/unactivation/reactivation
    license_server_url: str = "https://runsodin.com"

    # OIDC redirect URI override. When ODIN is behind a reverse proxy, set this
    # to the externally-visible callback URL (e.g. https://odin.example.com/api/auth/oidc/callback)
    # to avoid Host-header injection affecting the redirect_uri sent to the IdP.
    # If unset, redirect_uri is derived from request.base_url (default, fine for direct access).
    oidc_redirect_uri: Optional[str] = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
