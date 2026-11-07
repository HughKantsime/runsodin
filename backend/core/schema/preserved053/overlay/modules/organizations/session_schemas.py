"""Request schemas shared by organization session and quota routes."""
from typing import Optional

from pydantic import BaseModel as PydanticBaseModel


class QuotaUpdateRequest(PydanticBaseModel):
    quota_grams: Optional[float] = None
    quota_hours: Optional[float] = None
    quota_jobs: Optional[int] = None
    quota_period: Optional[str] = None
