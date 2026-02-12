"""Pydantic schemas for API request/response validation."""
from models import FilamentType

from datetime import datetime
from typing import Optional, List, Union, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict, computed_field, field_validator


# Re-define enums here to avoid circular imports
class JobStatus(str, Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OrderStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PARTIAL = "partial"
    FULFILLED = "fulfilled"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"




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
    api_key: Optional[str] = None
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
    camera_url: Optional[str] = None

    nickname: Optional[str] = None

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
    thumbnail_b64: Optional[str] = None
    notes: Optional[str] = None
    cost_per_item: Optional[float] = None
    units_per_bed: Optional[int] = 1
    quantity_per_bed: Optional[int] = 1  # Sellable pieces per print
    markup_percent: Optional[float] = 300
    is_favorite: Optional[bool] = False


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
    quantity_per_bed: Optional[int] = 1  # Sellable pieces per print
    markup_percent: Optional[float] = 300
    is_favorite: Optional[bool] = None


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
    priority: Union[int, str] = Field(default=3)
    duration_hours: Optional[float] = None
    colors_required: Optional[str] = None  # Comma-separated
    filament_type: Optional[FilamentType] = None
    notes: Optional[str] = None
    hold: bool = False
    due_date: Optional[datetime] = None


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
    
    # Order fulfillment
    order_item_id: Optional[int] = None
    quantity_on_bed: int = 1
    
    # Expanded relations (optional)
    printer: Optional[PrinterSummary] = None
    model: Optional[ModelResponse] = None


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


# ============== Product Schemas ==============

class ProductComponentBase(BaseModel):
    model_id: int
    quantity_needed: int = 1
    notes: Optional[str] = None


class ProductComponentCreate(ProductComponentBase):
    pass


class ProductComponentResponse(ProductComponentBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    product_id: int
    # Include model name for display
    model_name: Optional[str] = None


class ProductBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    sku: Optional[str] = None
    price: Optional[float] = None
    description: Optional[str] = None


class ProductCreate(ProductBase):
    components: Optional[List[ProductComponentCreate]] = None


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    sku: Optional[str] = None
    price: Optional[float] = None
    description: Optional[str] = None


class ProductConsumableBase(BaseModel):
    consumable_id: int
    quantity_per_product: float = 1
    notes: Optional[str] = None


class ProductConsumableCreate(ProductConsumableBase):
    pass


class ProductConsumableResponse(ProductConsumableBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    consumable_name: Optional[str] = None  # Populated by API


class ProductResponse(ProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    components: List[ProductComponentResponse] = []
    consumables: List[ProductConsumableResponse] = []

    # Calculated fields (populated by API)
    estimated_cogs: Optional[float] = None
    component_count: Optional[int] = None


class ProductSummary(BaseModel):
    """Lighter product response for dropdowns."""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    name: str
    sku: Optional[str] = None
    price: Optional[float] = None


# ============== Order Schemas ==============

class OrderItemBase(BaseModel):
    product_id: int
    quantity: int = 1
    unit_price: Optional[float] = None


class OrderItemCreate(OrderItemBase):
    pass


class OrderItemUpdate(BaseModel):
    quantity: Optional[int] = None
    unit_price: Optional[float] = None


class OrderItemResponse(OrderItemBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    order_id: int
    fulfilled_quantity: int = 0
    created_at: datetime
    
    # Include product name for display
    product_name: Optional[str] = None
    product_sku: Optional[str] = None
    
    # Calculated
    subtotal: Optional[float] = None
    is_fulfilled: Optional[bool] = None


class OrderBase(BaseModel):
    order_number: Optional[str] = None
    platform: Optional[str] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    order_date: Optional[datetime] = None
    notes: Optional[str] = None
    
    # Financials
    revenue: Optional[float] = None
    platform_fees: Optional[float] = None
    payment_fees: Optional[float] = None
    shipping_charged: Optional[float] = None
    shipping_cost: Optional[float] = None
    labor_minutes: Optional[int] = 0


class OrderCreate(OrderBase):
    items: Optional[List[OrderItemCreate]] = None


class OrderUpdate(BaseModel):
    order_number: Optional[str] = None
    platform: Optional[str] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    status: Optional[OrderStatus] = None
    order_date: Optional[datetime] = None
    shipped_date: Optional[datetime] = None
    tracking_number: Optional[str] = None
    notes: Optional[str] = None
    
    # Financials
    revenue: Optional[float] = None
    platform_fees: Optional[float] = None
    payment_fees: Optional[float] = None
    shipping_charged: Optional[float] = None
    shipping_cost: Optional[float] = None
    labor_minutes: Optional[int] = None


class OrderResponse(OrderBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    status: OrderStatus
    shipped_date: Optional[datetime] = None
    tracking_number: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    items: List[OrderItemResponse] = []
    
    # Calculated P&L fields (populated by API)
    total_items: Optional[int] = None
    fulfilled_items: Optional[int] = None
    estimated_cost: Optional[float] = None
    actual_cost: Optional[float] = None
    profit: Optional[float] = None
    margin_percent: Optional[float] = None
    jobs_total: Optional[int] = None
    jobs_complete: Optional[int] = None


class OrderSummary(BaseModel):
    """Lighter order response for lists."""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    order_number: Optional[str] = None
    platform: Optional[str] = None
    customer_name: Optional[str] = None
    status: OrderStatus
    revenue: Optional[float] = None
    order_date: Optional[datetime] = None
    item_count: int = 0
    fulfilled: bool = False


class OrderShipRequest(BaseModel):
    """Request to mark order as shipped."""
    tracking_number: Optional[str] = None
    shipped_date: Optional[datetime] = None  # Defaults to now


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


# ============== Alert Schemas (v0.17.0) ==============

class AlertTypeEnum(str, Enum):
    PRINT_COMPLETE = "print_complete"
    PRINT_FAILED = "print_failed"
    SPOOL_LOW = "spool_low"
    MAINTENANCE_OVERDUE = "maintenance_overdue"
    JOB_SUBMITTED = "job_submitted"
    JOB_APPROVED = "job_approved"
    JOB_REJECTED = "job_rejected"
    SPAGHETTI_DETECTED = "spaghetti_detected"
    FIRST_LAYER_ISSUE = "first_layer_issue"
    DETACHMENT_DETECTED = "detachment_detected"


class AlertSeverityEnum(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    user_id: int
    alert_type: AlertTypeEnum
    severity: AlertSeverityEnum
    title: str
    message: Optional[str] = None
    is_read: Optional[bool] = False
    is_dismissed: Optional[bool] = False
    printer_id: Optional[int] = None
    job_id: Optional[int] = None
    spool_id: Optional[int] = None
    metadata_json: Optional[Dict[str, Any]] = None
    created_at: datetime
    
    # Populated by API for display
    printer_name: Optional[str] = None
    job_name: Optional[str] = None
    spool_name: Optional[str] = None


class AlertSummary(BaseModel):
    """Aggregated alert counts for dashboard widget."""
    print_failed: int = 0
    spool_low: int = 0
    maintenance_overdue: int = 0
    total: int = 0


class AlertPreferenceBase(BaseModel):
    alert_type: AlertTypeEnum
    in_app: bool = True
    browser_push: bool = False
    email: bool = False
    threshold_value: Optional[float] = None


class AlertPreferenceResponse(AlertPreferenceBase):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    user_id: int


class AlertPreferencesUpdate(BaseModel):
    """Bulk update of all alert preferences for a user."""
    preferences: List[AlertPreferenceBase]


# ============== Telemetry & Nozzle Schemas ==============

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


# ============== Consumable Schemas ==============

class ConsumableBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    sku: Optional[str] = None
    unit: str = "piece"
    cost_per_unit: float = 0
    current_stock: float = 0
    min_stock: float = 0
    vendor: Optional[str] = None
    notes: Optional[str] = None
    status: str = "active"


class ConsumableCreate(ConsumableBase):
    pass


class ConsumableUpdate(BaseModel):
    name: Optional[str] = None
    sku: Optional[str] = None
    unit: Optional[str] = None
    cost_per_unit: Optional[float] = None
    current_stock: Optional[float] = None
    min_stock: Optional[float] = None
    vendor: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class ConsumableResponse(ConsumableBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    is_low_stock: Optional[bool] = None  # Populated by API


class ConsumableAdjust(BaseModel):
    """Manual stock adjustment."""
    quantity: float
    type: str = "restock"  # "restock" or "deduct"
    notes: Optional[str] = None


class SmtpConfigBase(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    from_address: str = ""
    use_tls: bool = True


class SmtpConfigResponse(BaseModel):
    """SMTP config response (password masked)."""
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    password_set: bool = False
    from_address: str = ""
    use_tls: bool = True


class PushSubscriptionCreate(BaseModel):
    endpoint: str
    p256dh_key: str
    auth_key: str
