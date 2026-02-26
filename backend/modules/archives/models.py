"""
modules/archives/models.py — ORM models for the archives domain.

Owns tables: timelapses
DUAL SCHEMA: also defined in docker/entrypoint.sh (TELEMETRYEOF). Keep in sync.
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.base import Base


class Timelapse(Base):
    """A timelapse video generated from camera frames during a print.
    DUAL SCHEMA: also defined in docker/entrypoint.sh (TELEMETRYEOF). Keep in sync."""
    __tablename__ = "timelapses"

    id = Column(Integer, primary_key=True)
    printer_id = Column(Integer, ForeignKey("printers.id"), nullable=False)
    print_job_id = Column(Integer, nullable=True)
    filename = Column(String(255), nullable=False)  # Relative path under /data/timelapses/
    frame_count = Column(Integer, default=0)
    duration_seconds = Column(Float, nullable=True)
    file_size_mb = Column(Float, nullable=True)
    status = Column(String(20), default="capturing")  # capturing, encoding, ready, failed
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)

    printer = relationship("Printer")
