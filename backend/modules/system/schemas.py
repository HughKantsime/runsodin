"""
modules/system/schemas.py — Pydantic schemas for the system domain.
"""

from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, ConfigDict


class MaintenanceTaskBase(BaseModel):
    name: str
    description: Optional[str] = None
    printer_model_filter: Optional[str] = None
    interval_print_hours: Optional[float] = None
    interval_days: Optional[int] = None
    estimated_cost: float = 0
    estimated_downtime_min: int = 30
    is_active: bool = True


class MaintenanceTaskCreate(MaintenanceTaskBase):
    pass


class MaintenanceTaskUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    printer_model_filter: Optional[str] = None
    interval_print_hours: Optional[float] = None
    interval_days: Optional[int] = None
    estimated_cost: Optional[float] = None
    estimated_downtime_min: Optional[int] = None
    is_active: Optional[bool] = None


class MaintenanceTaskResponse(MaintenanceTaskBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class MaintenanceLogBase(BaseModel):
    printer_id: int
    task_id: Optional[int] = None
    task_name: str
    performed_by: Optional[str] = None
    notes: Optional[str] = None
    cost: float = 0
    downtime_minutes: int = 0
    print_hours_at_service: float = 0


class MaintenanceLogCreate(MaintenanceLogBase):
    pass


class MaintenanceLogResponse(MaintenanceLogBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    performed_at: datetime


# ============== General / Core Schemas ==============

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
