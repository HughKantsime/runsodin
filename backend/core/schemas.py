"""
core/schemas.py — Core/general Pydantic schemas.
"""

from typing import List, Any
from pydantic import BaseModel, ValidationInfo
from pydantic_core import PydanticUseDefault


def reject_explicit_null(value, info: ValidationInfo):
    if value is None:
        raise ValueError(f"{info.field_name.replace('_', ' ')} cannot be null")
    return value


def read_null_as_default(value):
    if value is None:
        raise PydanticUseDefault()
    return value


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
