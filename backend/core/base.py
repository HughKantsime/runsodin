"""
core/base.py — Declarative Base and shared enums.

All ORM models import Base from here.
All shared enums (used across multiple domain modules) live here
to avoid circular imports between domain modules.
"""

from enum import Enum
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# SQLAlchemy 2.x defaults to using enum member NAMES as DB values.
# We want member VALUES (lowercase strings) instead.
_ENUM_VALUES = lambda x: [e.value for e in x]


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
    BED_COOLED = "bed_cooled"
    QUEUE_ADDED = "queue_added"
    QUEUE_SKIPPED = "queue_skipped"
    QUEUE_FAILED_START = "queue_failed_start"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# Hygroscopic materials that benefit from drying (shared constant)
HYGROSCOPIC_TYPES = {
    "PA", "NYLON_CF", "NYLON_GF", "PPS", "PPS_CF",
    "PETG", "PETG_CF", "PC", "PC_ABS", "PC_CF", "TPU", "PVA",
}
