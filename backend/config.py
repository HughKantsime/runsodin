"""
Configuration settings for PrintFarm Scheduler.

Loads from environment variables with sensible defaults.
"""

from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment."""
    
    # Database
    database_url: str = "sqlite:///./printfarm.db"
    
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
    
    # Frontend
    cors_origins: str = "*"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
