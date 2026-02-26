"""
Database models for O.D.I.N.

Re-export facade — all ORM classes now live in domain modules.
This file preserves backwards compatibility: `from models import Printer, Job, ...`
continues to work throughout the codebase.

Core entities:
- Printer: Physical printer with filament slots
- FilamentSlot: What's loaded in each AMS slot
- Model: Print model definitions with color/filament requirements
- Job: Individual print jobs in the queue

SCHEMA SPLIT:
  Tables defined ONLY here (via SQLAlchemy Base.metadata.create_all):
    printers, filament_slots, spools, spool_usage, drying_logs, models, jobs,
    scheduler_runs, filament_library, maintenance_tasks, maintenance_logs,
    products, product_components, orders, order_items, system_config,
    audit_logs, alerts, alert_preferences, push_subscriptions, print_presets

  Tables defined ONLY in docker/entrypoint.sh (raw SQL, not in SQLAlchemy):
    users, api_tokens, active_sessions, token_blacklist, quota_usage,
    model_revisions, groups, report_schedules, print_jobs, print_files,
    oidc_config, oidc_pending_states, oidc_auth_codes, webhooks,
    ams_telemetry, printer_telemetry, hms_error_history, login_attempts

  DUAL SCHEMA — defined in BOTH places (keep in sync!):
    vision_detections, vision_settings, vision_models, timelapses,
    nozzle_lifecycle, consumables, product_consumables, consumable_usage

  The main.py lifespan includes a drift check that logs warnings if
  SQLAlchemy columns diverge from the live PRAGMA table_info schema.
"""

# ── Core base ──────────────────────────────────────────────────────────────
from core.base import (
    Base,
    _ENUM_VALUES,
    JobStatus,
    OrderStatus,
    FilamentType,
    SpoolStatus,
    AlertType,
    AlertSeverity,
    HYGROSCOPIC_TYPES,
)

# ── Printers domain ────────────────────────────────────────────────────────
from modules.printers.models import (
    Printer,
    FilamentSlot,
    NozzleLifecycle,
)

# ── Jobs domain ────────────────────────────────────────────────────────────
from modules.jobs.models import (
    Job,
    SchedulerRun,
    PrintPreset,
)

# ── Inventory domain ───────────────────────────────────────────────────────
from modules.inventory.models import (
    Spool,
    FilamentLibrary,
    SpoolUsage,
    DryingLog,
    Consumable,
    ProductConsumable,
    ConsumableUsage,
)

# ── Models library domain ──────────────────────────────────────────────────
from modules.models_library.models import (
    Model,
)

# ── Vision domain ──────────────────────────────────────────────────────────
from modules.vision.models import (
    VisionDetection,
    VisionSettings,
    VisionModel,
)

# ── Notifications domain ───────────────────────────────────────────────────
from modules.notifications.models import (
    Alert,
    AlertPreference,
    PushSubscription,
)

# ── Orders domain ──────────────────────────────────────────────────────────
from modules.orders.models import (
    Order,
    OrderItem,
    Product,
    ProductComponent,
)

# ── Archives domain ────────────────────────────────────────────────────────
from modules.archives.models import (
    Timelapse,
)

# ── System domain ──────────────────────────────────────────────────────────
from modules.system.models import (
    MaintenanceTask,
    MaintenanceLog,
)

# ── Core models ────────────────────────────────────────────────────────────
from core.models import (
    SystemConfig,
    AuditLog,
)

__all__ = [
    # Base + enums
    "Base",
    "_ENUM_VALUES",
    "JobStatus",
    "OrderStatus",
    "FilamentType",
    "SpoolStatus",
    "AlertType",
    "AlertSeverity",
    "HYGROSCOPIC_TYPES",
    # Printers
    "Printer",
    "FilamentSlot",
    "NozzleLifecycle",
    # Jobs
    "Job",
    "SchedulerRun",
    "PrintPreset",
    # Inventory
    "Spool",
    "FilamentLibrary",
    "SpoolUsage",
    "DryingLog",
    "Consumable",
    "ProductConsumable",
    "ConsumableUsage",
    # Models library
    "Model",
    # Vision
    "VisionDetection",
    "VisionSettings",
    "VisionModel",
    # Notifications
    "Alert",
    "AlertPreference",
    "PushSubscription",
    # Orders
    "Order",
    "OrderItem",
    "Product",
    "ProductComponent",
    # Archives
    "Timelapse",
    # System
    "MaintenanceTask",
    "MaintenanceLog",
    # Core
    "SystemConfig",
    "AuditLog",
]
