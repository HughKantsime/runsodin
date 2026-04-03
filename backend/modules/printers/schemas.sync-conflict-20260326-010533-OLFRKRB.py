"""
modules/printers/schemas.py — Pydantic schemas for the printers domain.
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict, field_validator

from core.base import FilamentType


# ============== Filament Slot Schemas ==============

class FilamentSlotBase(BaseModel):
    slot_number: int = Field(..., ge=1)  # No upper bound — Bambu virtual trays use 513+
    filament_type: FilamentType = FilamentType.PLA
    color: Optional[str] = None
    color_hex: Optional[str] = None
    spoolman_spool_id: Optional[int] = None
    assigned_spool_id: Optional[int] = None
    spool_confirmed: Optional[bool] = None


class FilamentSlotCreate(FilamentSlotBase):
    pass


class FilamentSlotUpdate(BaseModel):
    filament_type: Optional[FilamentType] = None
    color: Optional[str] = None
    color_hex: Optional[str] = None
    spoolman_spool_id: Optional[int] = None
    assigned_spool_id: Optional[int] = None
    spool_confirmed: Optional[bool] = None


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
    # api_key is intentionally excluded — write-only credential, never returned in responses
    camera_url: Optional[str] = None
    nickname: Optional[str] = None
    # Live telemetry
    bed_temp: Optional[float] = None
    bed_target_temp: Optional[float] = None
    nozzle_temp: Optional[float] = None
    nozzle_target_temp: Optional[float] = None
    gcode_state: Optional[str] = None
    print_stage: Optional[str] = None
    hms_errors: Optional[str] = None
    lights_on: Optional[bool] = None
    nozzle_type: Optional[str] = None
    nozzle_diameter: Optional[float] = None
    fan_speed: Optional[int] = None
    bed_x_mm: Optional[float] = None
    bed_y_mm: Optional[float] = None
    last_seen: Optional[datetime] = None
    # Care counters (universal)
    total_print_hours: Optional[float] = None
    total_print_count: Optional[int] = None
    hours_since_maintenance: Optional[float] = None
    prints_since_maintenance: Optional[int] = None
    # Error tracking (universal)
    last_error_code: Optional[str] = None
    last_error_message: Optional[str] = None
    last_error_at: Optional[datetime] = None
    # Camera auto-discovery
    camera_discovered: Optional[bool] = None
    # Tags
    tags: List[str] = []
    # Timelapse
    timelapse_enabled: bool = False
    # Machine type (H2D, X1C, P1S, etc.)
    machine_type: Optional[str] = None


class PrinterCreate(PrinterBase):
    api_key: Optional[str] = None  # Write-only credential
    initial_slots: Optional[List[FilamentSlotCreate]] = None
    shared: bool = False


class PrinterUpdate(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    slot_count: Optional[int] = Field(default=None, ge=1, le=256)
    is_active: Optional[bool] = None
    api_type: Optional[str] = None
    api_host: Optional[str] = None
    api_key: Optional[str] = None
    camera_url: Optional[str] = None
    nickname: Optional[str] = None
    tags: Optional[List[str]] = None
    timelapse_enabled: Optional[bool] = None
    shared: Optional[bool] = None
    bed_x_mm: Optional[float] = None
    bed_y_mm: Optional[float] = None


class PrinterResponse(PrinterBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    filament_slots: List[FilamentSlotResponse] = []
    loaded_colors: List[str] = []
    tags: List[str] = []
    shared: bool = False
    org_id: Optional[int] = None

    @field_validator('camera_url', mode='before')
    @classmethod
    def _sanitize_camera_url(cls, v):
        """Strip credentials from RTSP camera URLs before returning to clients."""
        import re
        if not v:
            return v
        return re.sub(r'(rtsps?://)([^@]+)@', r'\1***@', v)


class PrinterSummary(BaseModel):
    """Lighter printer response for lists."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    model: Optional[str] = None
    is_active: bool
    loaded_colors: List[str] = []


# ============== Nozzle Lifecycle Schemas ==============

class NozzleLifecycleBase(BaseModel):
    nozzle_type: Optional[str] = None
    nozzle_diameter: Optional[float] = None
    notes: Optional[str] = None


class NozzleInstall(NozzleLifecycleBase):
    pass


class NozzleLifecycleResponse(NozzleLifecycleBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    printer_id: int
    installed_at: datetime
    removed_at: Optional[datetime] = None
    print_hours_accumulated: float = 0
    print_count: int = 0


# ============== Telemetry Schemas ==============

class TelemetryDataPoint(BaseModel):
    recorded_at: datetime
    bed_temp: Optional[float] = None
    nozzle_temp: Optional[float] = None
    bed_target: Optional[float] = None
    nozzle_target: Optional[float] = None
    fan_speed: Optional[int] = None


class HmsErrorHistoryEntry(BaseModel):
    id: int
    printer_id: int
    code: str
    message: Optional[str] = None
    severity: str = "warning"
    source: str = "bambu_hms"
    occurred_at: datetime


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
