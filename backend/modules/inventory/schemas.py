"""
modules/inventory/schemas.py — Pydantic schemas for the inventory domain.
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict

from core.base import FilamentType, SpoolStatus


# ============== Spool Schemas ==============

class SpoolBase(BaseModel):
    filament_id: int
    qr_code: Optional[str] = None
    rfid_tag: Optional[str] = None
    color_hex: Optional[str] = None
    initial_weight_g: float = 1000.0
    remaining_weight_g: float = 1000.0
    spool_weight_g: float = 250.0
    price: Optional[float] = None
    purchase_date: Optional[datetime] = None
    vendor: Optional[str] = None
    lot_number: Optional[str] = None
    status: SpoolStatus = SpoolStatus.ACTIVE
    location_printer_id: Optional[int] = None
    location_slot: Optional[int] = None
    storage_location: Optional[str] = None
    notes: Optional[str] = None
    pa_profile: Optional[str] = None
    low_stock_threshold_g: int = 50
    spoolman_spool_id: Optional[int] = None


class SpoolCreate(SpoolBase):
    pass


class SpoolUpdate(BaseModel):
    remaining_weight_g: Optional[float] = None
    status: Optional[SpoolStatus] = None
    location_printer_id: Optional[int] = None
    location_slot: Optional[int] = None
    storage_location: Optional[str] = None
    notes: Optional[str] = None
    pa_profile: Optional[str] = None
    low_stock_threshold_g: Optional[int] = None
    rfid_tag: Optional[str] = None
    color_hex: Optional[str] = None


class SpoolResponse(SpoolBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    percent_remaining: float = 0
    filament_name: Optional[str] = None
    filament_material: Optional[str] = None
    filament_brand: Optional[str] = None
    filament_color_hex: Optional[str] = None


# ============== FilamentLibrary Schemas ==============

class FilamentLibraryBase(BaseModel):
    brand: str
    name: str
    material: str = "PLA"
    color_hex: Optional[str] = None
    cost_per_gram: Optional[float] = None
    is_custom: bool = False


class FilamentLibraryCreate(FilamentLibraryBase):
    pass


class FilamentLibraryUpdate(BaseModel):
    brand: Optional[str] = None
    name: Optional[str] = None
    material: Optional[str] = None
    color_hex: Optional[str] = None
    cost_per_gram: Optional[float] = None


class FilamentLibraryResponse(FilamentLibraryBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# ============== Consumable Schemas ==============

class ConsumableBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    sku: Optional[str] = None
    unit: str = "piece"
    cost_per_unit: float = 0
    current_stock: float = 0
    min_stock: float = 0
    vendor: Optional[str] = None
    notes: Optional[str] = None
    status: str = "active"


class ConsumableCreate(ConsumableBase):
    pass


class ConsumableUpdate(BaseModel):
    name: Optional[str] = None
    sku: Optional[str] = None
    unit: Optional[str] = None
    cost_per_unit: Optional[float] = None
    current_stock: Optional[float] = None
    min_stock: Optional[float] = None
    vendor: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class ConsumableResponse(ConsumableBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    is_low_stock: Optional[bool] = None  # Populated by API


class ConsumableAdjust(BaseModel):
    """Manual stock adjustment."""
    quantity: float
    type: str = "restock"  # "restock" or "deduct"
    notes: Optional[str] = None


# ============== ProductConsumable Schemas ==============

class ProductConsumableBase(BaseModel):
    consumable_id: int
    quantity_per_product: float = 1
    notes: Optional[str] = None


class ProductConsumableCreate(ProductConsumableBase):
    pass


class ProductConsumableResponse(ProductConsumableBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    consumable_name: Optional[str] = None  # Populated by API
