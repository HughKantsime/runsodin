"""
modules/models_library/schemas.py — Pydantic schemas for the models library domain.
"""

from datetime import datetime
from typing import Optional, List, Dict
from pydantic import BaseModel, Field, ConfigDict, computed_field

from core.base import FilamentType


# ============== Model Schemas ==============

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
        if not self.cost_per_item or not self.markup_percent:
            return None
        return round(self.cost_per_item * (self.markup_percent / 100) * (self.units_per_bed or 1), 2)

    @computed_field
    @property
    def value_per_hour(self) -> Optional[float]:
        if not self.value_per_bed or not self.build_time_hours:
            return None
        return round(self.value_per_bed / self.build_time_hours, 2)

    total_filament_grams: float = 0
