"""Pydantic schemas for API request/response validation."""
from models import FilamentType

from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict, computed_field


# Re-define enums here to avoid circular imports
class JobStatus(str, Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"




# ============== Filament Slot Schemas ==============

class FilamentSlotBase(BaseModel):
    slot_number: int = Field(..., ge=1, le=16)
    filament_type: FilamentType = FilamentType.PLA
    color: Optional[str] = None
    color_hex: Optional[str] = None
    spoolman_spool_id: Optional[int] = None


class FilamentSlotCreate(FilamentSlotBase):
    pass


class FilamentSlotUpdate(BaseModel):
    filament_type: Optional[FilamentType] = None
    color: Optional[str] = None
    color_hex: Optional[str] = None
    spoolman_spool_id: Optional[int] = None


class FilamentSlotResponse(FilamentSlotBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    printer_id: int
    loaded_at: Optional[datetime] = None


# ============== Printer Schemas ==============

class PrinterBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    model: Optional[str] = None
    slot_count: int = Field(default=4, ge=1, le=16)
    is_active: bool = True
    api_type: Optional[str] = None
    api_host: Optional[str] = None
    api_key: Optional[str] = None


class PrinterCreate(PrinterBase):
    # Initial filament configuration
    initial_slots: Optional[List[FilamentSlotCreate]] = None


class PrinterUpdate(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    slot_count: Optional[int] = None
    is_active: Optional[bool] = None
    api_type: Optional[str] = None
    api_host: Optional[str] = None
    api_key: Optional[str] = None


class PrinterResponse(PrinterBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    created_at: datetime
    updated_at: datetime
    filament_slots: List[FilamentSlotResponse] = []
    loaded_colors: List[str] = []


class PrinterSummary(BaseModel):
    """Lighter printer response for lists."""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    name: str
    model: Optional[str] = None
    is_active: bool
    loaded_colors: List[str] = []


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
    notes: Optional[str] = None
    cost_per_item: Optional[float] = None
    units_per_bed: Optional[int] = 1
    markup_percent: Optional[float] = 300


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
    units_per_bed: Optional[int] = 1
    markup_percent: Optional[float] = 300


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


# ============== Job Schemas ==============

class JobBase(BaseModel):
    item_name: str = Field(..., min_length=1, max_length=200)
    model_id: Optional[int] = None
    quantity: int = Field(default=1, ge=1)
    priority: int = Field(default=3, ge=1, le=5)
    duration_hours: Optional[float] = None
    colors_required: Optional[str] = None  # Comma-separated
    filament_type: Optional[FilamentType] = None
    notes: Optional[str] = None
    hold: bool = False


class JobCreate(JobBase):
    pass


class JobUpdate(BaseModel):
    item_name: Optional[str] = None
    model_id: Optional[int] = None
    quantity: Optional[int] = None
    priority: Optional[int] = None
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


class JobResponse(JobBase):
    model_config = ConfigDict(from_attributes=True)
    
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
    
    # Expanded relations (optional)
    printer: Optional[PrinterSummary] = None
    model: Optional[ModelResponse] = None


class JobSummary(BaseModel):
    """Lighter job response for timeline views."""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    item_name: str
    status: JobStatus
    priority: int
    printer_id: Optional[int] = None
    printer_name: Optional[str] = None
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    duration_hours: float
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
    is_setup: bool = False  # True for color change blocks
    colors: List[str] = []


class TimelineResponse(BaseModel):
    """Full timeline view data."""
    start_date: datetime
    end_date: datetime
    slot_duration_minutes: int = 30
    printers: List[PrinterSummary]
    slots: List[TimelineSlot]


# ============== Spoolman Integration ==============

class SpoolmanSpool(BaseModel):
    """Spool data from Spoolman API."""
    id: int
    filament_name: str
    filament_type: str
    color_name: Optional[str] = None
    color_hex: Optional[str] = None
    remaining_weight: Optional[float] = None
    

class SpoolmanSyncResult(BaseModel):
    """Result of syncing with Spoolman."""
    success: bool
    spools_found: int
    slots_updated: int
    message: str


# ============== General ==============

class HealthCheck(BaseModel):
    status: str = "ok"
    version: str
    database: str
    spoolman_connected: bool = False


class PaginatedResponse(BaseModel):
    """Generic paginated response wrapper."""
    items: List[Any]
    total: int
    page: int
    page_size: int
    pages: int
