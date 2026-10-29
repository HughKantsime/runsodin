"""
modules/printers/schemas.py — Pydantic schemas for the printers domain.
"""

from datetime import datetime
from typing import Literal, Optional, List
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

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
    remaining: Optional[float] = None
    remaining_weight: Optional[float] = None  # grams left on the assigned spool
    spool_change_pending: bool = False
    observed_state: Optional[str] = None
    preset: Optional[str] = None
    brand: Optional[str] = None
    material_type: Optional[str] = None
    display_name: Optional[str] = None
    mapping_status: Optional[str] = None
    external_spool_id: Optional[str | int] = None

    @model_validator(mode='before')
    @classmethod
    def _slot_spool_grams(cls, data):
        """From a FilamentSlot row: add the assigned spool's remaining grams."""
        if isinstance(data, dict) or not hasattr(data, 'assigned_spool'):
            return data
        values = {name: getattr(data, name) for name in cls.model_fields if name != 'remaining_weight' and hasattr(data, name)}
        spool = data.assigned_spool
        from core.tray_identity import decode, preset_label
        state = decode(data.tray_observation)
        values['spool_change_pending'] = state.get('pending', False)
        values['observed_state'] = state.get('id')
        # A missing initial weight prevents a percentage, not a known gram
        # balance on an operator-managed non-Bambu spool (spec item 1).
        unknown_bambu = data.printer and data.printer.api_type == 'bambu' and data.remaining is None
        values['remaining_weight'] = float(spool.remaining_weight_g) if not unknown_bambu and spool is not None and spool.remaining_weight_g is not None else None
        if state:
            values['preset'] = preset_label(state)
            values['brand'] = spool.filament.brand if spool and spool.filament else None
            values['color'] = f'{spool.filament.brand} {spool.filament.name}'.strip() if spool and spool.filament else values['preset']
            if state.get('observed', {}).get('exists') is False:
                values.update(color=None, brand=None, preset=None)
        return values


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
    installed_nozzles: Optional[list[dict]] = None
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

    @model_validator(mode='after')
    def default_bambu_bed(self):
        from modules.printers.printer_models import BAMBU_BEDS, bambu_model_name
        dims = BAMBU_BEDS.get(bambu_model_name(self.model)) if self.api_type == 'bambu' else None
        if dims and self.bed_x_mm is None and self.bed_y_mm is None:
            self.bed_x_mm, self.bed_y_mm = dims
        return self


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


class PrinterMaintenance(BaseModel):
    state: Literal['ok', 'warn', 'hold'] = 'ok'
    note: str = ''
    set_by: Optional[int] = None
    set_by_name: Optional[str] = None
    set_at: Optional[str] = None


class PrinterResponse(PrinterBase):
    maintenance: PrinterMaintenance = Field(default_factory=PrinterMaintenance)
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    filament_slots: List[FilamentSlotResponse] = []
    filament_source: Optional[str] = None
    filament_source_status: Optional[str] = None
    loaded_colors: List[str] = []
    tags: List[str] = []
    shared: bool = False
    org_id: Optional[int] = None
    has_api_key: bool = False  # Safe boolean — never exposes the actual credential
    # NULL is reported as disabled: go2rtc and /cameras only stream TRUE.
    camera_enabled: bool = True
    # camera_url is the camera actually streamed: the configured URL, or the one
    # generated from credentials (Bambu RTSPS or a credential-free chamber marker). Always masked.
    camera_source: Optional[str] = None  # 'configured' | 'auto' | None

    @model_validator(mode='before')
    @classmethod
    def _effective_camera(cls, data):
        """From a Printer row: report the effective camera and where it comes from."""
        if isinstance(data, dict) or not hasattr(data, 'api_type'):
            return data
        from modules.printers.route_utils import effective_camera_url
        values = {name: getattr(data, name) for name in cls.model_fields if name != 'camera_source' and hasattr(data, name)}
        url = effective_camera_url(data)
        values['camera_url'] = url
        values['camera_source'] = None if not url else ('configured' if data.camera_url else 'auto')
        return values

    @field_validator('camera_enabled', mode='before')
    @classmethod
    def _camera_enabled_flag(cls, v):
        return bool(v)

    @field_validator('camera_url', mode='before')
    @classmethod
    def _sanitize_camera_url(cls, v):
        """Strip credentials from RTSP camera URLs before returning to clients.

        Credentialed URLs are stored Fernet-encrypted; decrypt first so the
        client gets ``rtsp://***@host/...`` rather than ciphertext.
        """
        from modules.printers.route_utils import masked_camera_url
        return masked_camera_url(v)


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
    nozzle_type: Optional[str] = Field(default=None, max_length=20)
    nozzle_diameter: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator("nozzle_diameter", mode="before")
    @classmethod
    def reject_boolean_diameter(cls, value):
        if isinstance(value, bool):
            raise ValueError("Nozzle diameter must be a positive finite number")
        return value


class NozzleLifecycleResponse(NozzleLifecycleBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    printer_id: int
    installed_at: datetime
    removed_at: Optional[datetime] = None
    print_hours_accumulated: Optional[float] = 0
    print_count: Optional[int] = 0


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
