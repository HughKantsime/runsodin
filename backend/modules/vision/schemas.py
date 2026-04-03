"""
modules/vision/schemas.py — Pydantic schemas for the vision domain.
"""

from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, ConfigDict


class VisionDetectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    printer_id: int
    print_job_id: Optional[int] = None
    detection_type: str
    confidence: float
    status: str = "pending"
    frame_path: Optional[str] = None
    bbox_json: Optional[str] = None
    metadata_json: Optional[str] = None
    reviewed_by: Optional[int] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime


class VisionSettingsBase(BaseModel):
    enabled: int = 1
    spaghetti_enabled: int = 1
    spaghetti_threshold: float = 0.65
    first_layer_enabled: int = 1
    first_layer_threshold: float = 0.60
    detachment_enabled: int = 1
    detachment_threshold: float = 0.70
    build_plate_empty_enabled: int = 0
    build_plate_empty_threshold: float = 0.70
    auto_pause: int = 0
    capture_interval_sec: int = 10
    collect_training_data: int = 0


class VisionSettingsResponse(VisionSettingsBase):
    model_config = ConfigDict(from_attributes=True)

    printer_id: int
    updated_at: Optional[datetime] = None


class VisionModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    detection_type: str
    filename: str
    version: Optional[str] = None
    input_size: int = 640
    is_active: int = 0
    metadata_json: Optional[str] = None
    uploaded_at: datetime
