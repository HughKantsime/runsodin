"""
modules/jobs/models.py — ORM models for the jobs domain.

Owns tables: jobs, scheduler_runs, print_presets

Note: Job has ForeignKeys to printers.id and models.id — referenced by
table name string to avoid cross-module model imports.
"""

from datetime import datetime
from typing import List
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, Enum as SQLEnum, Text, JSON
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.base import Base, JobStatus, FilamentType, _ENUM_VALUES


class Job(Base):
    """
    A print job in the queue.

    This is the core scheduling unit.
    """
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)

    # What to print
    model_id = Column(Integer, ForeignKey("models.id"))
    model_revision_id = Column(Integer, nullable=True)
    item_name = Column(String(200), nullable=False)  # Can override model name
    quantity = Column(Integer, default=1)

    # Scheduling
    status = Column(SQLEnum(JobStatus, values_callable=_ENUM_VALUES), default=JobStatus.PENDING)
    priority = Column(Integer, default=3)  # 1 = highest, 5 = lowest

    # Assignment (filled by scheduler)
    printer_id = Column(Integer, ForeignKey("printers.id"))
    scheduled_start = Column(DateTime)
    scheduled_end = Column(DateTime)

    # Actual times (filled during/after print)
    actual_start = Column(DateTime)
    actual_end = Column(DateTime)

    # Duration in hours (can be overridden from model default)
    duration_hours = Column(Float)

    # Colors needed for this specific job (overrides model if set)
    # Stored as comma-separated: "black, white, red matte"
    colors_required = Column(String(500))

    # Filament type override
    filament_type = Column(SQLEnum(FilamentType, values_callable=_ENUM_VALUES))

    # Scheduler metrics
    match_score = Column(Integer)  # How well this matched the printer state

    # Flags
    is_locked = Column(Boolean, default=False)  # Don't reschedule
    hold = Column(Boolean, default=False)  # Temporarily hold from scheduling

    # Notes
    notes = Column(Text)

    # Cost tracking (calculated at job creation)
    estimated_cost = Column(Float, nullable=True)
    suggested_price = Column(Float, nullable=True)

    # Order fulfillment linkage
    order_item_id = Column(Integer, ForeignKey("order_items.id"), nullable=True)
    quantity_on_bed = Column(Integer, default=1)  # How many pieces this job produces
    due_date = Column(DateTime, nullable=True)

    # Job approval workflow (v0.18.0)
    submitted_by = Column(Integer, nullable=True)
    approved_by = Column(Integer, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejected_reason = Column(Text, nullable=True)

    # Failure logging (v0.18.0)
    fail_reason = Column(String(100), nullable=True)   # Category: spaghetti, adhesion, clog, etc.
    fail_notes = Column(Text, nullable=True)            # Freeform notes

    # Chargeback tracking
    charged_to_user_id = Column(Integer, nullable=True)
    charged_to_org_id = Column(Integer, nullable=True)

    # User preferences
    is_favorite = Column(Boolean, default=False)

    # Scheduler constraints
    required_tags = Column(JSON, default=list)  # Only schedule on printers with these tags
    target_type = Column(String(20), default="specific")  # specific, model, protocol
    target_filter = Column(String(100), nullable=True)  # machine_type or protocol name

    # Queue ordering
    queue_position = Column(Integer, nullable=True)

    # Metadata
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships (string-based to avoid cross-module import)
    model = relationship("Model", back_populates="jobs")
    printer = relationship("Printer", back_populates="jobs")

    @property
    def colors_list(self) -> List[str]:
        """Get colors as a list."""
        if not self.colors_required:
            if self.model:
                return self.model.required_colors
            return []
        return [c.strip().lower() for c in self.colors_required.split(",") if c.strip()]

    @property
    def effective_duration(self) -> float:
        """Get duration, falling back to model default."""
        if self.duration_hours:
            return self.duration_hours * self.quantity
        if self.model and self.model.build_time_hours:
            return self.model.build_time_hours * self.quantity
        return 1.0  # Default 1 hour if unknown

    def __repr__(self):
        return f"<Job {self.id}: {self.item_name} ({self.status.value})>"


class SchedulerRun(Base):
    """
    Log of scheduler executions for debugging and metrics.
    """
    __tablename__ = "scheduler_runs"

    id = Column(Integer, primary_key=True)
    run_at = Column(DateTime, server_default=func.now())

    # Metrics
    total_jobs = Column(Integer, default=0)
    scheduled_count = Column(Integer, default=0)
    skipped_count = Column(Integer, default=0)
    setup_blocks = Column(Integer, default=0)  # Color changes needed
    avg_match_score = Column(Float)
    avg_job_duration = Column(Float)

    # Debug info
    notes = Column(Text)


class PrintPreset(Base):
    """Reusable print job preset/template."""
    __tablename__ = "print_presets"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True, nullable=False)
    model_id = Column(Integer, ForeignKey("models.id"), nullable=True)
    item_name = Column(String(200))
    quantity = Column(Integer, default=1)
    priority = Column(Integer, default=3)
    duration_hours = Column(Float, nullable=True)
    colors_required = Column(String(500), nullable=True)
    filament_type = Column(SQLEnum(FilamentType, values_callable=_ENUM_VALUES), nullable=True)
    required_tags = Column(JSON, default=list)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    model = relationship("Model")
