"""
modules/jobs/schemas.py — Pydantic schemas for the jobs domain.
"""

from datetime import datetime
from typing import Optional, List, Union, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

from core.base import JobStatus, FilamentType


# ============== Job Schemas ==============

# Named priority levels accepted on input -> stored integer rank.
PRIORITY_LEVELS = {
    'urgent': 1, 'highest': 1, 'high': 2, 'normal': 3, 'medium': 3,
    'low': 4, 'lowest': 5,
}

class JobBase(BaseModel):
    item_name: str = Field(..., min_length=1, max_length=200)
    model_id: Optional[int] = None
    quantity: int = Field(default=1, ge=1, le=10000)
    # Stored as an integer rank (1 = highest ... 5 = lowest). Named levels
    # (urgent/high/normal/low) are accepted on input and normalized here so the
    # scheduler never sees TEXT priorities.
    priority: int = Field(default=3)
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
        """Normalize named levels to ranks and keep integer ranks within 0–10."""
        if isinstance(v, bool):
            raise ValueError('priority must be an integer rank or a named level')
        if isinstance(v, str):
            label = v.strip().lower()
            if label in PRIORITY_LEVELS:
                return PRIORITY_LEVELS[label]
            if label.isdigit():
                v = int(label)
            else:
                raise ValueError(
                    'priority must be an integer rank or one of: ' + ', '.join(PRIORITY_LEVELS)
                )
        if isinstance(v, int):
            if v < 0 or v > 10:
                raise ValueError('priority must be between 0 and 10')
        return v


class JobCreate(JobBase):
    model_revision_id: Optional[int] = None
    # Specific-printer target: with target_type='specific' the scheduler only
    # places this job on the chosen printer. None = any compatible printer.
    printer_id: Optional[int] = Field(default=None, ge=1)


class JobUpdate(BaseModel):
    ams_mapping: Optional[list[dict[str, Any]]] = None
    item_name: Optional[str] = None
    model_id: Optional[int] = None
    quantity: Optional[int] = Field(default=None, ge=1, le=10000)
    priority: Optional[int] = Field(default=None, ge=0, le=10)
    # Accepted only so PATCH can refuse it explicitly (422): status changes go
    # through the guarded lifecycle and approval endpoints.
    status: Optional[JobStatus] = None
    # The scheduler's current assignment, not a pin. On a pinned job it must equal
    # the pin (after any pinned_printer_id in the same PATCH), else 409.
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
    # Explicit specific-printer pin: a printer id pins (replacing a model or
    # protocol target), None clears a specific pin (model/protocol targets are
    # kept). Only this field changes a pin; printer_id never does.
    pinned_printer_id: Optional[int] = Field(default=None, ge=1)

    @field_validator('quantity', 'hold', 'is_locked', mode='before')
    @classmethod
    def require_explicit_operational_value(cls, value):
        if value is None:
            raise ValueError('Choose an explicit quantity, hold or lock value')
        return value


class _PinView:
    """Attribute view of a Job ORM row that adds the computed pin fields."""

    def __init__(self, job, pin, issue):
        self._job = job
        self.pinned_printer_id = pin
        self.pinned_printer_issue = issue

    def __getattr__(self, name):
        return getattr(self._job, name)


class JobResponse(JobBase):
    model_config = ConfigDict(from_attributes=True)

    # Legacy NULLs are unknown read values, never fabricated production facts.
    quantity: Optional[int] = Field(default=None, ge=1, le=10000)
    hold: Optional[bool] = None

    # Override: relax min_length for response — DB may contain empty strings from
    # MQTT auto-created jobs or legacy data. Input validation stays on JobBase.
    item_name: str = Field(..., max_length=200)

    @field_validator('priority', mode='before')
    @classmethod
    def validate_priority_range(cls, v):
        """Read path: coerce legacy TEXT priorities to ranks, never fail a row."""
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        if isinstance(v, str):
            label = v.strip().lower()
            if label.isdigit():
                return int(label)
            return PRIORITY_LEVELS.get(label, 3)
        return 3

    id: int
    status: JobStatus
    printer_id: Optional[int] = None
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    actual_start: Optional[datetime] = None
    actual_end: Optional[datetime] = None
    match_score: Optional[int] = None
    is_locked: Optional[bool] = None
    created_at: datetime
    updated_at: datetime
    colors_list: List[str] = []
    effective_duration: Optional[float] = None

    # Cost tracking
    estimated_cost: Optional[float] = None
    suggested_price: Optional[float] = None

    # Stored dispatch selection; read-only, with the job's existing visibility.
    ams_mapping: Optional[List[int]] = None

    # Model versioning
    model_revision_id: Optional[int] = None

    # Order fulfillment
    order_item_id: Optional[int] = None
    quantity_on_bed: Optional[int] = 1

    # Queue order set by drag-and-drop reorder (None = not manually placed)
    queue_position: Optional[int] = None

    # Explicit specific-printer pin (target_type 'specific' + target_filter
    # printer id) and, when it blocks scheduling, why
    # ('pinned_printer_missing' | 'pinned_printer_inactive').
    pinned_printer_id: Optional[int] = None
    pinned_printer_issue: Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def _expose_pin(cls, data):
        if isinstance(data, dict) or not hasattr(data, '__table__'):
            return data
        from sqlalchemy.orm import object_session
        from modules.jobs.pinning import job_pin, cached_job_issue
        # One printer read per session (cached), not one query per job row.
        return _PinView(data, job_pin(data), cached_job_issue(object_session(data), data))

    # Approval workflow. These describe the job row itself and are only
    # served on org-scoped job routes (same visibility as the job).
    submitted_by: Optional[int] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    rejected_reason: Optional[str] = None
    charged_to_org_id: Optional[int] = None

    # Failure logging
    fail_reason: Optional[str] = None
    fail_notes: Optional[str] = None

    # Expanded relations (optional) — local summary schemas to avoid cross-module imports
    printer: Optional["_PrinterSummary"] = None
    model: Optional["_ModelSummary"] = None


class JobDetailResponse(JobResponse):
    ams_preview: Optional[dict] = None
    ams_mapping_editable: bool = False


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
    def validate_priority_range(cls, v):
        """Read path: coerce legacy TEXT priorities to ranks, never fail a row."""
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        if isinstance(v, str):
            label = v.strip().lower()
            if label.isdigit():
                return int(label)
            return PRIORITY_LEVELS.get(label, 3)
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
    model_config = ConfigDict(strict=True)
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
