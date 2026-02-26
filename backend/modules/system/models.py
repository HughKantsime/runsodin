"""
modules/system/models.py — ORM models for the system domain.

Owns tables: maintenance_tasks, maintenance_logs
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, Text
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.base import Base


class MaintenanceTask(Base):
    """Template for a recurring maintenance task."""
    __tablename__ = "maintenance_tasks"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    printer_model_filter = Column(String(100), nullable=True)  # null = applies to all printers
    interval_print_hours = Column(Float, nullable=True)        # Service every X print hours
    interval_days = Column(Integer, nullable=True)             # Service every X calendar days
    estimated_cost = Column(Float, default=0)
    estimated_downtime_min = Column(Integer, default=30)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    logs = relationship("MaintenanceLog", back_populates="task")


class MaintenanceLog(Base):
    """Record of maintenance performed on a printer."""
    __tablename__ = "maintenance_logs"

    id = Column(Integer, primary_key=True)
    printer_id = Column(Integer, ForeignKey("printers.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("maintenance_tasks.id"), nullable=True)
    task_name = Column(String(200), nullable=False)
    performed_at = Column(DateTime, server_default=func.now())
    performed_by = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    cost = Column(Float, default=0)
    downtime_minutes = Column(Integer, default=0)
    print_hours_at_service = Column(Float, default=0)  # Odometer reading when serviced

    printer = relationship("Printer")
    task = relationship("MaintenanceTask", back_populates="logs")
