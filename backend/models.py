"""
Database models for O.D.I.N.

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

from datetime import datetime
from enum import Enum
from typing import Optional, List
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, Enum as SQLEnum, Text, JSON
)

# SQLAlchemy 2.x defaults to using enum member NAMES as DB values.
# We want member VALUES (lowercase strings) instead.
_ENUM_VALUES = lambda x: [e.value for e in x]
from sqlalchemy.orm import relationship, declarative_base, Session
from sqlalchemy.sql import func

Base = declarative_base()


class JobStatus(str, Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OrderStatus(str, Enum):
    """Status progression for orders."""
    PENDING = "pending"           # Order received, no jobs started
    IN_PROGRESS = "in_progress"   # At least 1 job printing/scheduled
    PARTIAL = "partial"           # Some jobs complete, not all
    FULFILLED = "fulfilled"       # All jobs complete, ready to ship
    SHIPPED = "shipped"           # Out the door, tracking entered
    CANCELLED = "cancelled"


class FilamentType(str, Enum):
    """
    Filament types including Bambu Lab codes (PLA-S, PLA-CF, PA-CF, etc.)
    """
    EMPTY = "empty"  # Slot has no filament loaded

    # === Standard Materials (backwards compatible) ===
    PLA = "PLA"
    PETG = "PETG"
    ABS = "ABS"
    ASA = "ASA"
    TPU = "TPU"
    PA = "PA"           # Nylon
    PC = "PC"           # Polycarbonate
    PVA = "PVA"
    OTHER = "OTHER"
    
    # === Bambu PLA Variants ===
    PLA_SUPPORT = "PLA_SUPPORT"    # Bambu PLA-S
    PLA_CF = "PLA_CF"              # Carbon Fiber PLA
    
    # === Bambu PETG Variants ===
    PETG_CF = "PETG_CF"            # Carbon Fiber PETG
    
    # === Bambu Nylon Variants ===
    NYLON_CF = "NYLON_CF"          # PA-CF, PA6-CF
    NYLON_GF = "NYLON_GF"          # PA-GF
    
    # === Bambu PC Variants ===
    PC_ABS = "PC_ABS"
    PC_CF = "PC_CF"
    
    # === Support Materials ===
    SUPPORT = "SUPPORT"
    HIPS = "HIPS"
    
    # === High Performance ===
    PPS = "PPS"
    PPS_CF = "PPS_CF"
    
    @classmethod
    def from_bambu_code(cls, bambu_code: str) -> 'FilamentType':
        """Convert Bambu code (e.g. PLA-S) to FilamentType."""
        if not bambu_code:
            return cls.OTHER
        mapping = {
            "PLA-S": cls.PLA_SUPPORT, "PLA-CF": cls.PLA_CF,
            "PETG-CF": cls.PETG_CF, "PA-CF": cls.NYLON_CF,
            "PA6-CF": cls.NYLON_CF, "PA-GF": cls.NYLON_GF,
            "PC-ABS": cls.PC_ABS, "PC-CF": cls.PC_CF,
            "PPS-CF": cls.PPS_CF,
        }
        normalized = bambu_code.upper().strip()
        if normalized in mapping:
            return mapping[normalized]
        try:
            return cls(normalized)
        except ValueError:
            return cls.OTHER



class SpoolStatus(str, Enum):
    ACTIVE = "active"
    EMPTY = "empty"
    ARCHIVED = "archived"

class Printer(Base):
    """
    A physical 3D printer in the farm.
    
    Tracks basic info and current filament state.
    """
    __tablename__ = "printers"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)  # e.g., "X1C", "P1S-01"
    nickname = Column(String(100), nullable=True)  # Friendly display name
    model = Column(String(100))  # e.g., "Bambu Lab X1 Carbon"
    slot_count = Column(Integer, default=4)  # Number of AMS slots
    is_active = Column(Boolean, default=True)  # Available for scheduling
    display_order = Column(Integer, default=0)  # For manual ordering in UI
    camera_url = Column(String, nullable=True)  # RTSP camera URL
    camera_enabled = Column(Boolean, default=True)  # Whether to show camera
    
    # Optional: for future printer API integration
    api_type = Column(String(50))  # "bambu", "octoprint", "moonraker", etc.
    api_host = Column(String(255))
    api_key = Column(String(255))
    
    # User preferences
    is_favorite = Column(Boolean, default=False)
    
    # Heartbeat
    last_seen = Column(DateTime, nullable=True)

    # Live telemetry (updated by MQTT/Moonraker monitors)
    bed_temp = Column(Float, nullable=True)
    bed_target_temp = Column(Float, nullable=True)
    nozzle_temp = Column(Float, nullable=True)
    nozzle_target_temp = Column(Float, nullable=True)
    gcode_state = Column(String(20), nullable=True)
    print_stage = Column(String(50), nullable=True)
    hms_errors = Column(Text, nullable=True)
    lights_on = Column(Boolean, nullable=True)
    lights_toggled_at = Column(DateTime, nullable=True)
    nozzle_type = Column(String(20), nullable=True)
    nozzle_diameter = Column(Float, nullable=True)
    fan_speed = Column(Integer, nullable=True)
    bed_x_mm = Column(Float, nullable=True)
    bed_y_mm = Column(Float, nullable=True)

    # Smart plug integration
    plug_type = Column(String(20), nullable=True)         # 'tasmota', 'homeassistant', 'mqtt'
    plug_host = Column(String(255), nullable=True)        # IP or HA URL
    plug_entity_id = Column(String(255), nullable=True)   # HA entity_id or MQTT topic
    plug_auth_token = Column(Text, nullable=True)         # HA long-lived access token
    plug_auto_on = Column(Boolean, default=True)          # Auto power-on before print
    plug_auto_off = Column(Boolean, default=True)         # Auto power-off after print
    plug_cooldown_minutes = Column(Integer, default=5)    # Delay before auto-off
    plug_power_state = Column(Boolean, nullable=True)     # Last known power state
    plug_energy_kwh = Column(Float, default=0)            # Cumulative energy
    
    # Care counters (universal - tracked internally for all printer types)
    total_print_hours = Column(Float, default=0)
    total_print_count = Column(Integer, default=0)
    hours_since_maintenance = Column(Float, default=0)
    prints_since_maintenance = Column(Integer, default=0)
    
    # Error tracking (universal)
    last_error_code = Column(String(50), nullable=True)
    last_error_message = Column(Text, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    
    # Camera auto-discovery
    camera_discovered = Column(Boolean, default=False)

    # Tags for fleet organization
    tags = Column(JSON, default=list)  # ["Room A", "PLA-only", "Production"]

    # Timelapse
    timelapse_enabled = Column(Boolean, default=False)

    # H2D / machine variant detection
    machine_type = Column(String(20), nullable=True)  # "X1C", "P1S", "H2D", etc.

    # Organization scoping
    org_id = Column(Integer, nullable=True)
    shared = Column(Boolean, default=False)

    # Metadata
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    filament_slots = relationship("FilamentSlot", back_populates="printer", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="printer")
    
    @property
    def loaded_colors(self) -> List[str]:
        """Get list of currently loaded colors."""
        return [slot.color.lower() for slot in self.filament_slots if slot.color]
    
    def __repr__(self):
        return f"<Printer {self.name}>"


class FilamentSlot(Base):
    """
    A single filament slot on a printer (e.g., AMS slot 1-4).
    
    Tracks what's currently loaded.
    """
    __tablename__ = "filament_slots"
    
    id = Column(Integer, primary_key=True)
    printer_id = Column(Integer, ForeignKey("printers.id"), nullable=False)
    slot_number = Column(Integer, nullable=False)  # 1-4 typically
    
    # What's loaded
    filament_type = Column(SQLEnum(FilamentType, values_callable=_ENUM_VALUES), default=FilamentType.EMPTY)
    color = Column(String(50))  # e.g., "black", "white", "red matte"
    color_hex = Column(String(7))  # e.g., "#FF0000" for UI display
    
    # Spoolman integration
    spoolman_spool_id = Column(Integer)  # Link to Spoolman spool
    
    # Local spool tracking
    assigned_spool_id = Column(Integer, ForeignKey("spools.id"), nullable=True)
    spool_confirmed = Column(Boolean, default=False)
    
    # Metadata
    loaded_at = Column(DateTime, server_default=func.now())
    
    # Relationships
    printer = relationship("Printer", back_populates="filament_slots")
    assigned_spool = relationship("Spool", foreign_keys=[assigned_spool_id])
    
    def __repr__(self):
        return f"<FilamentSlot {self.printer_id}:{self.slot_number} - {self.color}>"


class Spool(Base):
    """Individual physical spool of filament."""
    __tablename__ = "spools"
    
    id = Column(Integer, primary_key=True)
    filament_id = Column(Integer, ForeignKey("filament_library.id"), nullable=False)
    qr_code = Column(String(50), unique=True, index=True)
    rfid_tag = Column(String(32), unique=True, index=True, nullable=True)  # Bambu RFID tag_uid
    color_hex = Column(String(6), nullable=True)  # Actual color from AMS
    
    # Weight tracking
    initial_weight_g = Column(Float, default=1000.0)
    remaining_weight_g = Column(Float, default=1000.0)
    spool_weight_g = Column(Float, default=250.0)
    
    # Purchase info
    price = Column(Float)
    purchase_date = Column(DateTime)
    vendor = Column(String(100))
    lot_number = Column(String(50))
    
    # Status
    status = Column(SQLEnum(SpoolStatus, values_callable=_ENUM_VALUES), default=SpoolStatus.ACTIVE)
    
    # Location
    location_printer_id = Column(Integer, ForeignKey("printers.id"), nullable=True)
    location_slot = Column(Integer, nullable=True)
    storage_location = Column(String(100))
    notes = Column(Text)

    # Organization scoping
    org_id = Column(Integer, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    # Relationships
    filament = relationship("FilamentLibrary", back_populates="spools")
    printer = relationship("Printer", foreign_keys=[location_printer_id])
    usage_history = relationship("SpoolUsage", back_populates="spool", cascade="all, delete-orphan")
    
    @property
    def percent_remaining(self) -> float:
        if self.initial_weight_g and self.initial_weight_g > 0 and self.remaining_weight_g is not None:
            return (self.remaining_weight_g / self.initial_weight_g) * 100
        return 0


class AuditLog(Base):
    """Audit log for tracking user actions."""
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, server_default=func.now())
    action = Column(String(50), nullable=False)  # e.g., "create", "update", "delete", "sync"
    entity_type = Column(String(50))  # e.g., "printer", "spool", "job"
    entity_id = Column(Integer)
    details = Column(JSON)  # Additional context
    ip_address = Column(String(45))  # IPv4 or IPv6


class SpoolUsage(Base):
    """Record of filament usage from a spool."""
    __tablename__ = "spool_usage"
    
    id = Column(Integer, primary_key=True)
    spool_id = Column(Integer, ForeignKey("spools.id"), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    weight_used_g = Column(Float, nullable=False)
    used_at = Column(DateTime, server_default=func.now())
    notes = Column(String(255))
    
    # Relationships
    spool = relationship("Spool", back_populates="usage_history")
    job = relationship("Job")



class DryingLog(Base):
    """Record of a filament drying session."""
    __tablename__ = "drying_logs"

    id = Column(Integer, primary_key=True)
    spool_id = Column(Integer, ForeignKey("spools.id"), nullable=False)
    dried_at = Column(DateTime, server_default=func.now())
    duration_hours = Column(Float, nullable=False)
    temp_c = Column(Float, nullable=True)
    method = Column(String(50), default="dryer")  # dryer, oven, desiccant
    notes = Column(Text, nullable=True)

    spool = relationship("Spool", backref="drying_history")


# Hygroscopic materials that benefit from drying
HYGROSCOPIC_TYPES = {
    "PA", "NYLON_CF", "NYLON_GF", "PPS", "PPS_CF",
    "PETG", "PETG_CF", "PC", "PC_ABS", "PC_CF", "TPU", "PVA",
}


class Model(Base):
    """
    A 3D model that can be printed.
    
    Stores metadata and filament requirements per color slot.
    """
    __tablename__ = "models"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)  # e.g., "Crocodile (Mini Critter)"
    
    # Print characteristics
    build_time_hours = Column(Float)  # Estimated print time
    default_filament_type = Column(SQLEnum(FilamentType, values_callable=_ENUM_VALUES), default=FilamentType.PLA)
    
    # Filament usage per color slot (stored as JSON for flexibility)
    # Format: {"color1": {"color": "black", "grams": 17}, "color2": {...}}
    color_requirements = Column(JSON, default=dict)
    
    # For display/organization
    category = Column(String(100))  # e.g., "Mini Critters", "Retail Display"
    thumbnail_url = Column(String(500))
    thumbnail_b64 = Column(Text)  # Base64 thumbnail from .3mf
    print_file_id = Column(Integer)  # Link to print_files if auto-created
    notes = Column(Text)
    
    # Pricing (optional, from your Pricing sheet)
    cost_per_item = Column(Float)
    units_per_bed = Column(Integer, default=1)
    quantity_per_bed = Column(Integer, default=1)  # Sellable pieces per print (from object checklist)
    markup_percent = Column(Float, default=300)
    
    # User preferences
    is_favorite = Column(Boolean, default=False)

    # Organization scoping
    org_id = Column(Integer, nullable=True)

    # Metadata
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    jobs = relationship("Job", back_populates="model")
    
    @property
    def required_colors(self) -> List[str]:
        """Get list of colors needed for this model."""
        if not self.color_requirements:
            return []
        return [c.get("color", "").lower() for c in self.color_requirements.values() if c.get("color")]
    
    @property
    def total_filament_grams(self) -> float:
        """Total filament usage in grams."""
        if not self.color_requirements:
            return 0
        return sum(c.get("grams", 0) for c in self.color_requirements.values())
    
    def __repr__(self):
        return f"<Model {self.name}>"


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

    # Queue ordering
    queue_position = Column(Integer, nullable=True)

    # Metadata
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
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


class Timelapse(Base):
    """A timelapse video generated from camera frames during a print.
    DUAL SCHEMA: also defined in docker/entrypoint.sh (TELEMETRYEOF). Keep in sync."""
    __tablename__ = "timelapses"

    id = Column(Integer, primary_key=True)
    printer_id = Column(Integer, ForeignKey("printers.id"), nullable=False)
    print_job_id = Column(Integer, nullable=True)
    filename = Column(String(255), nullable=False)  # Relative path under /data/timelapses/
    frame_count = Column(Integer, default=0)
    duration_seconds = Column(Float, nullable=True)
    file_size_mb = Column(Float, nullable=True)
    status = Column(String(20), default="capturing")  # capturing, encoding, ready, failed
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)

    printer = relationship("Printer")


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


class FilamentLibrary(Base):
    """Built-in filament library for users without Spoolman."""
    __tablename__ = "filament_library"
    
    id = Column(Integer, primary_key=True, index=True)
    brand = Column(String, nullable=False)
    name = Column(String, nullable=False)
    material = Column(String, default="PLA")
    color_hex = Column(String(6))
    cost_per_gram = Column(Float, nullable=True)  # Per-material pricing ($/g)
    is_custom = Column(Boolean, default=False)  # User-added vs built-in
    created_at = Column(DateTime, server_default=func.now())
    
    # Relationships
    spools = relationship("Spool", back_populates="filament")



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


class NozzleLifecycle(Base):
    """Track nozzle installs, retirements, and accumulated usage.
    DUAL SCHEMA: also defined in docker/entrypoint.sh (TELEMETRYEOF). Keep in sync."""
    __tablename__ = "nozzle_lifecycle"

    id = Column(Integer, primary_key=True)
    printer_id = Column(Integer, ForeignKey("printers.id"), nullable=False)
    nozzle_type = Column(String(20), nullable=True)
    nozzle_diameter = Column(Float, nullable=True)
    installed_at = Column(DateTime, server_default=func.now())
    removed_at = Column(DateTime, nullable=True)
    print_hours_accumulated = Column(Float, default=0)
    print_count = Column(Integer, default=0)
    notes = Column(Text, nullable=True)

    printer = relationship("Printer")


# ============================================================
# Orders, Products & BOM (v0.14.0)
# ============================================================

class Product(Base):
    """
    A sellable product in your catalog.
    
    Can be simple (single print) or complex (multiple prints/assembly).
    The Bill of Materials (BOM) is stored in ProductComponent.
    """
    __tablename__ = "products"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    sku = Column(String(50), nullable=True)  # Optional SKU/part number
    price = Column(Float, nullable=True)      # Default selling price
    description = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    # Relationships
    components = relationship("ProductComponent", back_populates="product", cascade="all, delete-orphan")
    order_items = relationship("OrderItem", back_populates="product")
    
    def __repr__(self):
        return f"<Product {self.name}>"


class ProductComponent(Base):
    """
    Bill of Materials entry - links a Model to a Product.
    
    Example: Golf Ball Dispenser needs 1x base, 1x tube, 4x brackets, 4x feet.
    Each of those is a ProductComponent with quantity_needed set.
    """
    __tablename__ = "product_components"
    
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    model_id = Column(Integer, ForeignKey("models.id"), nullable=False)
    quantity_needed = Column(Integer, default=1)  # How many of this part per product
    notes = Column(Text, nullable=True)
    
    # Relationships
    product = relationship("Product", back_populates="components")
    model = relationship("Model")
    
    def __repr__(self):
        return f"<ProductComponent {self.product_id}:{self.model_id} x{self.quantity_needed}>"


class Order(Base):
    """
    A customer purchase/order.
    
    Can come from Etsy, Amazon, wholesale, direct sales, etc.
    Contains line items (OrderItem) and tracks fulfillment status.
    """
    __tablename__ = "orders"
    
    id = Column(Integer, primary_key=True)
    order_number = Column(String(100), nullable=True)  # External ref (Etsy order #)
    platform = Column(String(50), nullable=True)       # 'etsy', 'amazon', 'direct', 'wholesale'
    
    # Customer info
    customer_name = Column(String(200), nullable=True)
    customer_email = Column(String(200), nullable=True)
    
    # Status tracking
    status = Column(SQLEnum(OrderStatus, values_callable=_ENUM_VALUES), default=OrderStatus.PENDING)
    
    # Financials - what you charged
    revenue = Column(Float, nullable=True)             # Total charged to customer
    platform_fees = Column(Float, nullable=True)       # Etsy/Amazon cut
    payment_fees = Column(Float, nullable=True)        # Stripe/PayPal cut
    shipping_charged = Column(Float, nullable=True)    # What customer paid for shipping
    shipping_cost = Column(Float, nullable=True)       # What you paid for label
    
    # Labor tracking (order-level for MVP)
    labor_minutes = Column(Integer, default=0)         # Packing/shipping labor
    
    # Notes
    notes = Column(Text, nullable=True)
    
    # Dates
    order_date = Column(DateTime, nullable=True)
    shipped_date = Column(DateTime, nullable=True)
    tracking_number = Column(String(100), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    # Relationships
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Order {self.order_number or self.id} ({self.status.value})>"


class OrderItem(Base):
    """
    A line item on an order.
    
    Example: "2x Baby Yoda @ $15 each"
    Tracks fulfillment progress as jobs complete.
    """
    __tablename__ = "order_items"
    
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    
    quantity = Column(Integer, default=1)              # How many ordered
    unit_price = Column(Float, nullable=True)          # Price per unit on THIS order
    fulfilled_quantity = Column(Integer, default=0)    # How many completed so far
    
    # Timestamps
    created_at = Column(DateTime, server_default=func.now())
    
    # Relationships
    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")
    jobs = relationship("Job", backref="order_item")
    
    def __repr__(self):
        return f"<OrderItem {self.order_id}:{self.product_id} x{self.quantity}>"


# ============================================================
# Consumables (non-printed inventory)
# ============================================================

class Consumable(Base):
    """Non-printed item in inventory (hardware, packaging, labels, etc.).
    DUAL SCHEMA: also defined in docker/entrypoint.sh (CONSUMABLESEOF). Keep in sync."""
    __tablename__ = "consumables"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    sku = Column(String(50), nullable=True)
    unit = Column(String(20), default="piece")  # piece, gram, ml, meter, pack
    cost_per_unit = Column(Float, default=0)
    current_stock = Column(Float, default=0)
    min_stock = Column(Float, default=0)
    vendor = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)
    status = Column(String(20), default="active")  # active, depleted, discontinued
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    product_links = relationship("ProductConsumable", back_populates="consumable")
    usage_history = relationship("ConsumableUsage", back_populates="consumable")


class ProductConsumable(Base):
    """BOM entry linking a consumable to a product (parallel to ProductComponent).
    DUAL SCHEMA: also defined in docker/entrypoint.sh (CONSUMABLESEOF). Keep in sync."""
    __tablename__ = "product_consumables"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    consumable_id = Column(Integer, ForeignKey("consumables.id"), nullable=False)
    quantity_per_product = Column(Float, default=1)
    notes = Column(Text, nullable=True)

    product = relationship("Product", backref="consumable_links")
    consumable = relationship("Consumable", back_populates="product_links")


class ConsumableUsage(Base):
    """Audit trail for consumable stock changes.
    DUAL SCHEMA: also defined in docker/entrypoint.sh (CONSUMABLESEOF). Keep in sync."""
    __tablename__ = "consumable_usage"

    id = Column(Integer, primary_key=True)
    consumable_id = Column(Integer, ForeignKey("consumables.id"), nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    quantity_used = Column(Float, nullable=False)
    used_at = Column(DateTime, server_default=func.now())
    notes = Column(String(255), nullable=True)

    consumable = relationship("Consumable", back_populates="usage_history")
    order = relationship("Order")


class SystemConfig(Base):
    """Key-value config store for system settings (RBAC, etc.)."""
    __tablename__ = "system_config"

    key = Column(String(100), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# ============================================================
# Vigil AI Vision Tables
# ============================================================

class VisionDetection(Base):
    """AI print failure detection record.
    DUAL SCHEMA: also defined in docker/entrypoint.sh (VISIONEOF). Keep in sync."""
    __tablename__ = "vision_detections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    printer_id = Column(Integer, ForeignKey("printers.id"), nullable=False)
    print_job_id = Column(Integer, nullable=True)
    detection_type = Column(Text, nullable=False)
    confidence = Column(Float, nullable=False)
    status = Column(Text, default="pending")
    frame_path = Column(Text, nullable=True)
    bbox_json = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    reviewed_by = Column(Integer, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    printer = relationship("Printer", foreign_keys=[printer_id])


class VisionSettings(Base):
    """Per-printer vision monitoring settings.
    DUAL SCHEMA: also defined in docker/entrypoint.sh (VISIONEOF). Keep in sync."""
    __tablename__ = "vision_settings"

    printer_id = Column(Integer, ForeignKey("printers.id"), primary_key=True)
    enabled = Column(Integer, default=1)
    spaghetti_enabled = Column(Integer, default=1)
    spaghetti_threshold = Column(Float, default=0.65)
    first_layer_enabled = Column(Integer, default=1)
    first_layer_threshold = Column(Float, default=0.60)
    detachment_enabled = Column(Integer, default=1)
    detachment_threshold = Column(Float, default=0.70)
    build_plate_empty_enabled = Column(Integer, default=0)
    build_plate_empty_threshold = Column(Float, default=0.70)
    auto_pause = Column(Integer, default=0)
    capture_interval_sec = Column(Integer, default=10)
    collect_training_data = Column(Integer, default=0)
    updated_at = Column(DateTime, server_default=func.now())


class VisionModel(Base):
    """Registered ONNX model for vision detection.
    DUAL SCHEMA: also defined in docker/entrypoint.sh (VISIONEOF). Keep in sync."""
    __tablename__ = "vision_models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    detection_type = Column(Text, nullable=False)
    filename = Column(Text, nullable=False)
    version = Column(Text, nullable=True)
    input_size = Column(Integer, default=640)
    is_active = Column(Integer, default=0)
    metadata_json = Column(Text, nullable=True)
    uploaded_at = Column(DateTime, server_default=func.now())


# ============================================================
# Alerts & Notifications
# ============================================================

class AlertType(str, Enum):
    """Types of alerts the system can generate."""
    PRINT_COMPLETE = "print_complete"
    PRINT_FAILED = "print_failed"
    PRINTER_ERROR = "printer_error"
    SPOOL_LOW = "spool_low"
    MAINTENANCE_OVERDUE = "maintenance_overdue"
    JOB_SUBMITTED = "job_submitted"
    JOB_APPROVED = "job_approved"
    JOB_REJECTED = "job_rejected"
    SPAGHETTI_DETECTED = "spaghetti_detected"
    FIRST_LAYER_ISSUE = "first_layer_issue"
    DETACHMENT_DETECTED = "detachment_detected"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Alert(Base):
    """
    Individual alert/notification instance.
    
    Created by the alert dispatcher when an event triggers.
    Each user gets their own alert record based on their preferences.
    """
    __tablename__ = "alerts"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)  # References users table (raw SQL)
    
    # Alert classification
    alert_type = Column(SQLEnum(AlertType, values_callable=_ENUM_VALUES), nullable=False)
    severity = Column(SQLEnum(AlertSeverity, values_callable=_ENUM_VALUES), nullable=False)
    
    # Content
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=True)
    
    # State
    is_read = Column(Boolean, default=False, index=True)
    is_dismissed = Column(Boolean, default=False)
    
    # Optional references to related entities
    printer_id = Column(Integer, ForeignKey("printers.id"), nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    spool_id = Column(Integer, ForeignKey("spools.id"), nullable=True)
    
    # Flexible extra data
    metadata_json = Column(JSON, nullable=True)
    
    # Timestamp
    created_at = Column(DateTime, server_default=func.now(), index=True)
    
    # Relationships
    printer = relationship("Printer", foreign_keys=[printer_id])
    job = relationship("Job", foreign_keys=[job_id])
    spool = relationship("Spool", foreign_keys=[spool_id])
    
    def __repr__(self):
        return f"<Alert {self.id}: {self.alert_type.value} for user {self.user_id}>"


class AlertPreference(Base):
    """
    Per-user, per-alert-type channel configuration.
    """
    __tablename__ = "alert_preferences"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    alert_type = Column(SQLEnum(AlertType, values_callable=_ENUM_VALUES), nullable=False)
    
    # Delivery channels
    in_app = Column(Boolean, default=True)
    browser_push = Column(Boolean, default=False)
    email = Column(Boolean, default=False)
    
    # Configurable threshold
    threshold_value = Column(Float, nullable=True)
    
    def __repr__(self):
        return f"<AlertPreference user={self.user_id} type={self.alert_type.value}>"


class PushSubscription(Base):
    """Browser push notification subscription."""
    __tablename__ = "push_subscriptions"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    endpoint = Column(Text, nullable=False)
    p256dh_key = Column(Text, nullable=False)
    auth_key = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    
    def __repr__(self):
        return f"<PushSubscription user={self.user_id}>"
