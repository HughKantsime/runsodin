"""
Database models for PrintFarm Scheduler

Core entities:
- Printer: Physical printer with filament slots
- FilamentSlot: What's loaded in each AMS slot
- Model: Print model definitions with color/filament requirements
- Job: Individual print jobs in the queue
"""

from datetime import datetime
from enum import Enum
from typing import Optional, List
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, 
    ForeignKey, Enum as SQLEnum, Text, JSON, create_engine
)
from sqlalchemy.orm import relationship, declarative_base, Session
from sqlalchemy.sql import func

Base = declarative_base()


class JobStatus(str, Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FilamentType(str, Enum):
    """
    Filament types including Bambu Lab codes (PLA-S, PLA-CF, PA-CF, etc.)
    """
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
    model = Column(String(100))  # e.g., "Bambu Lab X1 Carbon"
    slot_count = Column(Integer, default=4)  # Number of AMS slots
    is_active = Column(Boolean, default=True)  # Available for scheduling
    display_order = Column(Integer, default=0)  # For manual ordering in UI
    
    # Optional: for future printer API integration
    api_type = Column(String(50))  # "bambu", "octoprint", "moonraker", etc.
    api_host = Column(String(255))
    api_key = Column(String(255))
    
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
    filament_type = Column(SQLEnum(FilamentType), default=FilamentType.PLA)
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
    status = Column(SQLEnum(SpoolStatus), default=SpoolStatus.ACTIVE)
    
    # Location
    location_printer_id = Column(Integer, ForeignKey("printers.id"), nullable=True)
    location_slot = Column(Integer, nullable=True)
    storage_location = Column(String(100))
    notes = Column(Text)
    
    # Timestamps
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    # Relationships
    filament = relationship("FilamentLibrary", back_populates="spools")
    printer = relationship("Printer", foreign_keys=[location_printer_id])
    usage_history = relationship("SpoolUsage", back_populates="spool", cascade="all, delete-orphan")
    
    @property
    def percent_remaining(self) -> float:
        if self.initial_weight_g and self.initial_weight_g > 0:
            return (self.remaining_weight_g / self.initial_weight_g) * 100
        return 0


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
    default_filament_type = Column(SQLEnum(FilamentType), default=FilamentType.PLA)
    
    # Filament usage per color slot (stored as JSON for flexibility)
    # Format: {"color1": {"color": "black", "grams": 17}, "color2": {...}}
    color_requirements = Column(JSON, default=dict)
    
    # For display/organization
    category = Column(String(100))  # e.g., "Mini Critters", "Retail Display"
    thumbnail_url = Column(String(500))
    notes = Column(Text)
    
    # Pricing (optional, from your Pricing sheet)
    cost_per_item = Column(Float)
    units_per_bed = Column(Integer, default=1)
    markup_percent = Column(Float, default=300)
    
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
    item_name = Column(String(200), nullable=False)  # Can override model name
    quantity = Column(Integer, default=1)
    
    # Scheduling
    status = Column(SQLEnum(JobStatus), default=JobStatus.PENDING)
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
    filament_type = Column(SQLEnum(FilamentType))
    
    # Scheduler metrics
    match_score = Column(Integer)  # How well this matched the printer state
    
    # Flags
    is_locked = Column(Boolean, default=False)  # Don't reschedule
    hold = Column(Boolean, default=False)  # Temporarily hold from scheduling
    
    # Notes
    notes = Column(Text)
    
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


# Database initialization helper
def init_db(database_url: str = "sqlite:///./printfarm.db"):
    """Create all tables and return engine."""
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(engine)
    return engine

class FilamentLibrary(Base):
    """Built-in filament library for users without Spoolman."""
    __tablename__ = "filament_library"
    
    id = Column(Integer, primary_key=True, index=True)
    brand = Column(String, nullable=False)
    name = Column(String, nullable=False)
    material = Column(String, default="PLA")
    color_hex = Column(String(6))
    is_custom = Column(Boolean, default=False)  # User-added vs built-in
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    spools = relationship("Spool", back_populates="filament")
