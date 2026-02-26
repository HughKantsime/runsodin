"""
modules/printers/models.py — ORM models for the printers domain.

Owns tables: printers, filament_slots, nozzle_lifecycle
"""

from datetime import datetime
from typing import List
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, Enum as SQLEnum, Text, JSON
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.base import Base, FilamentType, _ENUM_VALUES


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
