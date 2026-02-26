"""
modules/jobs/schemas.py — Pydantic schemas for the jobs domain.
"""

from datetime import datetime
from typing import Optional, List, Union, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator

from core.base import JobStatus, FilamentType


# ============== Job Schemas ==============

class JobBase(BaseModel):
    item_name: str = Field(..., min_length=1, max_length=200)
    model_id: Optional[int] = None
    quantity: int = Field(default=1, ge=1, le=10000)
    priority: Union[int, str] = Field(default=3)
    duration_hours: Optional[float] = None
    colors_required: Optional[str] = None  # Comma-separated
    filament_type: Optional[FilamentType] = None
    notes: Optional[str] = None
    hold: bool = False
    due_date: Optional[datetime] = None
    required_tags: Optional[List[str]] = []
    target_type: Optional[str] = "specific"  # specific, model, protocol
    target_filter: Optional[str] = None  # machine_type or protocol name when target_type != specific

    @field_validator('required_tags', mode='before')
    @classmethod
    def coerce_tags_none(cls, v):
        """DB stores NULL for empty tags — coerce to empty list."""
        if v is None:
            return []
        return v

    @field_validator('priority', mode='before')
    @classmethod
    def validate_priority_range(cls, v):
        """Clamp integer priorities to 0–10; leave strings for normalize_priority."""
        if isinstance(v, int) and not isinstance(v, bool):
            if v < 0 or v > 10:
                raise ValueError('priority must be between 0 and 10')
        return v


class JobCreate(JobBase):
    model_revision_id: Optional[int] = None


class JobUpdate(BaseModel):
    item_name: Optional[str] = None
    model_id: Optional[int] = None
    quantity: Optional[int] = Field(default=None, ge=1, le=10000)
    priority: Optional[int] = Field(default=None, ge=0, le=10)
    status: Optional[JobStatus] = None
    printer_id: Optional[int] = None
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    duration_hours: Optional[float] = None
    colors_required: Optional[str] = None
    filament_type: Optional[FilamentType] = None
    notes: Optional[str] = None
    hold: Optional[bool] = None
    is_locked: Optional[bool] = None
    due_date: Optional[datetime] = None


class JobResponse(JobBase):
    model_config = ConfigDict(from_attributes=True)

    @field_validator('priority', mode='before')
    @classmethod
    def normalize_priority(cls, v):
        """Coerce string priorities to int for response serialization."""
        if isinstance(v, int):
            return v
        priority_map = {
            'urgent': 1, 'high': 2, 'normal': 3,
            'medium': 3, 'low': 4, 'lowest': 5,
        }
        if isinstance(v, str):
            return priority_map.get(v.lower(), 3)
        return 3

    id: int
    status: JobStatus
    printer_id: Optional[int] = None
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    actual_start: Optional[datetime] = None
    actual_end: Optional[datetime] = None
    match_score: Optional[int] = None
    is_locked: bool = False
    created_at: datetime
    updated_at: datetime
    colors_list: List[str] = []
    effective_duration: float = 1.0

    # Cost tracking
    estimated_cost: Optional[float] = None
    suggested_price: Optional[float] = None

    # Model versioning
    model_revision_id: Optional[int] = None

    # Order fulfillment
    order_item_id: Optional[int] = None
    quantity_on_bed: Optional[int] = 1

    # Expanded relations (optional) — local summary schemas to avoid cross-module imports
    printer: Optional["_PrinterSummary"] = None
    model: Optional["_ModelSummary"] = None


class _PrinterSummary(BaseModel):
    """Inline printer summary for job responses (avoids cross-module import)."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    model: Optional[str] = None
    is_active: bool = True
    loaded_colors: List[str] = []


class _ModelSummary(BaseModel):
    """Inline model summary for job responses (avoids cross-module import)."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


# Resolve forward refs
JobResponse.model_rebuild()


class JobSummary(BaseModel):
    """Lighter job response for timeline views."""
    model_config = ConfigDict(from_attributes=True)

    @field_validator('priority', mode='before')
    @classmethod
    def normalize_priority(cls, v):
        """Coerce string priorities to int for response serialization."""
        if isinstance(v, int):
            return v
        priority_map = {
            'urgent': 1, 'high': 2, 'normal': 3,
            'medium': 3, 'low': 4, 'lowest': 5,
        }
        if isinstance(v, str):
            return priority_map.get(v.lower(), 3)
        return 3

    id: int
    item_name: str
    status: JobStatus
    priority: Union[int, str]
    printer_id: Optional[int] = None
    printer_name: Optional[str] = None
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    duration_hours: Optional[float] = None
    colors_list: List[str] = []
    match_score: Optional[int] = None


# ============== Scheduler Schemas ==============

class SchedulerConfig(BaseModel):
    """Configuration for a scheduler run."""
    blackout_start: str = "22:30"  # HH:MM format
    blackout_end: str = "05:30"
    setup_duration_slots: int = 1  # 30-min slots for color change
    horizon_days: int = 7  # How far ahead to schedule


class SchedulerRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_at: datetime
    total_jobs: int
    scheduled_count: int
    skipped_count: int
    setup_blocks: int
    avg_match_score: Optional[float] = None
    avg_job_duration: Optional[float] = None
    notes: Optional[str] = None


class ScheduleResult(BaseModel):
    """Result of running the scheduler."""
    success: bool
    run_id: int
    scheduled: int
    skipped: int
    setup_blocks: int
    message: str
    jobs: List[JobSummary] = []


# ============== Timeline Schemas ==============

class TimelineSlot(BaseModel):
    """A single time slot on the timeline."""
    start: datetime
    end: datetime
    printer_id: int
    printer_name: str
    job_id: Optional[int] = None
    item_name: Optional[str] = None
    status: Optional[JobStatus] = None
    mqtt_job_id: Optional[int] = None
    is_setup: bool = False  # True for color change blocks
    colors: List[str] = []


class TimelineResponse(BaseModel):
    """Full timeline view data."""
    start_date: datetime
    end_date: datetime
    slot_duration_minutes: int = 30
    printers: List[Any] = []  # List[PrinterSummary] — avoid cross-domain circular import
    slots: List[TimelineSlot]
