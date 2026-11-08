"""
core/schemas.py — Core/general Pydantic schemas.
"""

from typing import List, Any
from pydantic import BaseModel


class HealthCheck(BaseModel):
    status: str = "ok"
    version: str
    database: str
    spoolman_connected: bool = False


class PaginatedResponse(BaseModel):
    """Generic paginated response wrapper."""
    items: List[Any]
    total: int
    page: int
    page_size: int
    pages: int
