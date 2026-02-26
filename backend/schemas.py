"""Pydantic schemas for API request/response validation.

Re-export facade — all Pydantic schema classes now live in domain modules.
This file preserves backwards compatibility: `from schemas import PrinterCreate, ...`
continues to work throughout the codebase.
"""

# Schema-layer enums — imported from core.base to avoid class duplication.
# Previously defined as separate copies in this file to avoid circular imports;
# now core.base is the single source of truth for all shared enums.
from core.base import JobStatus, OrderStatus


# ── Printers domain ────────────────────────────────────────────────────────
from modules.printers.schemas import (
    FilamentSlotBase,
    FilamentSlotCreate,
    FilamentSlotUpdate,
    FilamentSlotResponse,
    PrinterBase,
    PrinterCreate,
    PrinterUpdate,
    PrinterResponse,
    PrinterSummary,
    NozzleLifecycleBase,
    NozzleInstall,
    NozzleLifecycleResponse,
    TelemetryDataPoint,
    HmsErrorHistoryEntry,
    SpoolmanSpool,
    SpoolmanSyncResult,
)

# ── Models library domain ──────────────────────────────────────────────────
from modules.models_library.schemas import (
    ColorRequirement,
    ModelBase,
    ModelCreate,
    ModelUpdate,
    ModelResponse,
)

# ── Jobs domain ────────────────────────────────────────────────────────────
from modules.jobs.schemas import (
    JobBase,
    JobCreate,
    JobUpdate,
    JobResponse,
    JobSummary,
    SchedulerConfig,
    SchedulerRunResponse,
    ScheduleResult,
    TimelineSlot,
    TimelineResponse,
)

# ── Inventory domain ───────────────────────────────────────────────────────
from modules.inventory.schemas import (
    ConsumableBase,
    ConsumableCreate,
    ConsumableUpdate,
    ConsumableResponse,
    ConsumableAdjust,
    ProductConsumableBase,
    ProductConsumableCreate,
    ProductConsumableResponse,
)

# ── Orders domain ──────────────────────────────────────────────────────────
from modules.orders.schemas import (
    ProductComponentBase,
    ProductComponentCreate,
    ProductComponentResponse,
    ProductBase,
    ProductCreate,
    ProductUpdate,
    ProductResponse,
    ProductSummary,
    OrderItemBase,
    OrderItemCreate,
    OrderItemUpdate,
    OrderItemResponse,
    OrderBase,
    OrderCreate,
    OrderUpdate,
    OrderResponse,
    OrderSummary,
    OrderShipRequest,
)

# ── Notifications domain ───────────────────────────────────────────────────
from modules.notifications.schemas import (
    AlertTypeEnum,
    AlertSeverityEnum,
    AlertResponse,
    AlertSummary,
    AlertPreferenceBase,
    AlertPreferenceResponse,
    AlertPreferencesUpdate,
    PushSubscriptionCreate,
    SmtpConfigBase,
    SmtpConfigResponse,
)

# ── Core / general ─────────────────────────────────────────────────────────
from core.schemas import (
    HealthCheck,
    PaginatedResponse,
)

# Re-export FilamentType for callers that do `from schemas import FilamentType`
# (originally schemas.py imported this from models.py via `from models import FilamentType`)
from core.base import FilamentType  # noqa: F401 (already imported via JobStatus/OrderStatus import above)

__all__ = [
    # Schema-layer enums
    "JobStatus",
    "OrderStatus",
    # FilamentType (originally from models)
    "FilamentType",
    # Filament slots
    "FilamentSlotBase",
    "FilamentSlotCreate",
    "FilamentSlotUpdate",
    "FilamentSlotResponse",
    # Printers
    "PrinterBase",
    "PrinterCreate",
    "PrinterUpdate",
    "PrinterResponse",
    "PrinterSummary",
    # Nozzle lifecycle
    "NozzleLifecycleBase",
    "NozzleInstall",
    "NozzleLifecycleResponse",
    # Telemetry
    "TelemetryDataPoint",
    "HmsErrorHistoryEntry",
    # Spoolman
    "SpoolmanSpool",
    "SpoolmanSyncResult",
    # Models library
    "ColorRequirement",
    "ModelBase",
    "ModelCreate",
    "ModelUpdate",
    "ModelResponse",
    # Jobs
    "JobBase",
    "JobCreate",
    "JobUpdate",
    "JobResponse",
    "JobSummary",
    # Scheduler
    "SchedulerConfig",
    "SchedulerRunResponse",
    "ScheduleResult",
    # Timeline
    "TimelineSlot",
    "TimelineResponse",
    # Consumables
    "ConsumableBase",
    "ConsumableCreate",
    "ConsumableUpdate",
    "ConsumableResponse",
    "ConsumableAdjust",
    "ProductConsumableBase",
    "ProductConsumableCreate",
    "ProductConsumableResponse",
    # Products
    "ProductComponentBase",
    "ProductComponentCreate",
    "ProductComponentResponse",
    "ProductBase",
    "ProductCreate",
    "ProductUpdate",
    "ProductResponse",
    "ProductSummary",
    # Orders
    "OrderItemBase",
    "OrderItemCreate",
    "OrderItemUpdate",
    "OrderItemResponse",
    "OrderBase",
    "OrderCreate",
    "OrderUpdate",
    "OrderResponse",
    "OrderSummary",
    "OrderShipRequest",
    # Alerts / notifications
    "AlertTypeEnum",
    "AlertSeverityEnum",
    "AlertResponse",
    "AlertSummary",
    "AlertPreferenceBase",
    "AlertPreferenceResponse",
    "AlertPreferencesUpdate",
    "PushSubscriptionCreate",
    "SmtpConfigBase",
    "SmtpConfigResponse",
    # General
    "HealthCheck",
    "PaginatedResponse",
]
