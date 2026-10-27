"""
modules/models_library/schemas.py — Pydantic schemas for the models library domain.
"""

from datetime import datetime
from typing import Optional, List, Dict
from pydantic import BaseModel, Field, ConfigDict, computed_field

from core.base import FilamentType


# ============== Model Schemas ==============

class FileScheduleOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ams_mapping: Optional[list[dict]] = None


class ModelScheduleOptions(FileScheduleOptions):
    model_config = ConfigDict(extra="forbid")
    quantity: int = Field(default=1, ge=1, strict=True)
    priority: int = Field(default=5, ge=1, le=5, strict=True)
    due_date: Optional[datetime] = None
    notes: Optional[str] = None
    plate_index: Optional[int] = Field(default=None, ge=0, strict=True)
    print_file_id: Optional[int] = Field(default=None, ge=1, strict=True)
    payload_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")

class ColorRequirement(BaseModel):
    color: str
    grams: float = 0
    filament_type: Optional[FilamentType] = None


class ModelBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    build_time_hours: Optional[float] = None
    default_filament_type: FilamentType = FilamentType.PLA
    color_requirements: Optional[Dict[str, ColorRequirement]] = None
    category: Optional[str] = None
    thumbnail_url: Optional[str] = None
    thumbnail_b64: Optional[str] = None
    notes: Optional[str] = None
    cost_per_item: Optional[float] = None
    units_per_bed: Optional[int] = Field(default=1, ge=1, le=10000)
    quantity_per_bed: Optional[int] = Field(default=1, ge=1, le=10000)  # Sellable pieces per print
    markup_percent: Optional[float] = 300
    is_favorite: Optional[bool] = False


class ModelCreate(ModelBase):
    pass


class ModelUpdate(BaseModel):
    name: Optional[str] = None
    build_time_hours: Optional[float] = None
    default_filament_type: Optional[FilamentType] = None
    color_requirements: Optional[Dict[str, ColorRequirement]] = None
    category: Optional[str] = None
    thumbnail_url: Optional[str] = None
    notes: Optional[str] = None
    cost_per_item: Optional[float] = None
    units_per_bed: Optional[int] = Field(default=1, ge=1, le=10000)
    quantity_per_bed: Optional[int] = Field(default=1, ge=1, le=10000)  # Sellable pieces per print
    markup_percent: Optional[float] = 300
    is_favorite: Optional[bool] = None


class ModelResponse(ModelBase):
    model_config = ConfigDict(from_attributes=True)

    default_filament_type: Optional[FilamentType] = None
    id: int
    created_at: datetime
    updated_at: datetime
    required_colors: List[str] = []

    @computed_field
    @property
    def time_per_item(self) -> Optional[float]:
        if not self.build_time_hours or not self.units_per_bed:
            return None
        return round(self.build_time_hours / self.units_per_bed, 2)

    @computed_field
    @property
    def filament_per_item(self) -> Optional[float]:
        if not self.units_per_bed:
            return None
        return round(self.total_filament_grams / self.units_per_bed, 2)

    @computed_field
    @property
    def value_per_bed(self) -> Optional[float]:
        if self.cost_per_item is None or self.markup_percent is None:
            return None
        return round(self.cost_per_item * (self.markup_percent / 100) * (self.units_per_bed or 1), 2)

    @computed_field
    @property
    def value_per_hour(self) -> Optional[float]:
        if self.value_per_bed is None or not self.build_time_hours:
            return None
        return round(self.value_per_bed / self.build_time_hours, 2)

    total_filament_grams: float = 0
