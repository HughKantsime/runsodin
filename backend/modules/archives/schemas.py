"""
modules/archives/schemas.py — Pydantic schemas for the archives domain.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict


class TimelapseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    printer_id: int
    print_job_id: Optional[int] = None
    filename: str
    frame_count: int = 0
    duration_seconds: Optional[float] = None
    file_size_mb: Optional[float] = None
    status: str = "capturing"
    created_at: datetime
    completed_at: Optional[datetime] = None
