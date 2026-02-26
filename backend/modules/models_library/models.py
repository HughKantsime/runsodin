"""
modules/models_library/models.py — ORM models for the models library domain.

Owns tables: models
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
