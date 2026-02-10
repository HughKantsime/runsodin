"""
Configuration settings for PrintFarm Scheduler.

Loads from environment variables with sensible defaults.
"""

from typing import Optional, List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment."""
    
    # Database
    database_url: str = "sqlite:///./odin.db"
    
    # Spoolman integration (optional)
    spoolman_url: Optional[str] = None
    
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
    cors_origins: str = "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
