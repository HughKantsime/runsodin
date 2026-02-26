"""
modules/inventory/models.py — ORM models for the inventory domain.

Owns tables: spools, filament_library, spool_usage, drying_logs,
             consumables, product_consumables, consumable_usage
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, Enum as SQLEnum, Text, JSON
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.base import Base, FilamentType, SpoolStatus, _ENUM_VALUES


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

    # Pressure advance profile and low-stock threshold
    pa_profile = Column(String(50), nullable=True)  # K-factor or profile name
    low_stock_threshold_g = Column(Integer, default=50)

    # Spoolman link
    spoolman_spool_id = Column(Integer, nullable=True)

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
