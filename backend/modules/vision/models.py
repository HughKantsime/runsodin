"""
modules/vision/models.py — ORM models for the vision (Vigil AI) domain.

Owns tables: vision_detections, vision_settings, vision_models
DUAL SCHEMA: all three are also defined in docker/entrypoint.sh (VISIONEOF). Keep in sync.
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, Text
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.base import Base


class VisionDetection(Base):
    """AI print failure detection record.
    DUAL SCHEMA: also defined in docker/entrypoint.sh (VISIONEOF). Keep in sync."""
    __tablename__ = "vision_detections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    printer_id = Column(Integer, ForeignKey("printers.id"), nullable=False)
    print_job_id = Column(Integer, nullable=True)
    detection_type = Column(Text, nullable=False)
    confidence = Column(Float, nullable=False)
    status = Column(Text, default="pending")
    frame_path = Column(Text, nullable=True)
    bbox_json = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    reviewed_by = Column(Integer, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    printer = relationship("Printer", foreign_keys=[printer_id])


class VisionSettings(Base):
    """Per-printer vision monitoring settings.
    DUAL SCHEMA: also defined in docker/entrypoint.sh (VISIONEOF). Keep in sync."""
    __tablename__ = "vision_settings"

    printer_id = Column(Integer, ForeignKey("printers.id"), primary_key=True)
    enabled = Column(Integer, default=1)
    spaghetti_enabled = Column(Integer, default=1)
    spaghetti_threshold = Column(Float, default=0.65)
    first_layer_enabled = Column(Integer, default=1)
    first_layer_threshold = Column(Float, default=0.60)
    detachment_enabled = Column(Integer, default=1)
    detachment_threshold = Column(Float, default=0.70)
    build_plate_empty_enabled = Column(Integer, default=0)
    build_plate_empty_threshold = Column(Float, default=0.70)
    auto_pause = Column(Integer, default=0)
    capture_interval_sec = Column(Integer, default=10)
    collect_training_data = Column(Integer, default=0)
    updated_at = Column(DateTime, server_default=func.now())


class VisionModel(Base):
    """Registered ONNX model for vision detection.
    DUAL SCHEMA: also defined in docker/entrypoint.sh (VISIONEOF). Keep in sync."""
    __tablename__ = "vision_models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    detection_type = Column(Text, nullable=False)
    filename = Column(Text, nullable=False)
    version = Column(Text, nullable=True)
    input_size = Column(Integer, default=640)
    is_active = Column(Integer, default=0)
    metadata_json = Column(Text, nullable=True)
    uploaded_at = Column(DateTime, server_default=func.now())
