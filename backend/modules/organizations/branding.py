"""
Branding configuration for white-label deployments.

Singleton model storing app name, full color palette, fonts, logo, and footer.
Public GET endpoint (no auth - needed before login screen renders).
Admin-only PUT/POST/DELETE endpoints for updates.
"""

from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from datetime import datetime
import os

from core.base import Base


# ============== Model ==============

class Branding(Base):
    __tablename__ = "branding"

    id = Column(Integer, primary_key=True, default=1)

    # ---- Identity ----
    app_name = Column(String(100), default="O.D.I.N.")
    app_subtitle = Column(String(200), default="Scheduler")

    # ---- Color Palette ----
    # Accent / brand highlight (the print-* green by default)
    primary_color = Column(String(7), default="#22c55e")     # print-500
    accent_color = Column(String(7), default="#4ade80")      # print-400
    # Sidebar
    sidebar_bg = Column(String(7), default="#1a1917")        # farm-950
    sidebar_border = Column(String(7), default="#3b3934")    # farm-800
    sidebar_text = Column(String(7), default="#8a8679")      # farm-400
    sidebar_active_bg = Column(String(7), default="#3b3934") # farm-800
    sidebar_active_text = Column(String(7), default="#4ade80")  # print-400
    # Content area
    content_bg = Column(String(7), default="#1a1917")        # farm-950
    card_bg = Column(String(7), default="#33312d")           # farm-900
    card_border = Column(String(7), default="#3b3934")       # farm-800
    text_primary = Column(String(7), default="#e5e4e1")      # farm-100
    text_secondary = Column(String(7), default="#8a8679")    # farm-400
    text_muted = Column(String(7), default="#58554a")        # farm-600
    # Inputs / interactive
    input_bg = Column(String(7), default="#3b3934")          # farm-800
    input_border = Column(String(7), default="#47453d")      # farm-700

    # ---- Fonts ----
    font_display = Column(String(200), default="system-ui, -apple-system, sans-serif")
    font_body = Column(String(200), default="system-ui, -apple-system, sans-serif")
    font_mono = Column(String(200), default="ui-monospace, monospace")

    # ---- Assets ----
    logo_url = Column(String(500), nullable=True)
    favicon_url = Column(String(500), nullable=True)

    # ---- Footer ----
    footer_text = Column(String(500), default="System Online")
    support_url = Column(String(500), nullable=True)

    # ---- Metadata ----
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


def get_or_create_branding(db: Session) -> "Branding":
    """Get the singleton branding record, creating defaults if needed."""
    branding = db.query(Branding).filter(Branding.id == 1).first()
    if not branding:
        branding = Branding(id=1)
        db.add(branding)
        db.commit()
        db.refresh(branding)
    return branding


# All columns that can be updated via API
UPDATABLE_FIELDS = [
    "app_name", "app_subtitle",
    "primary_color", "accent_color",
    "sidebar_bg", "sidebar_border", "sidebar_text",
    "sidebar_active_bg", "sidebar_active_text",
    "content_bg", "card_bg", "card_border",
    "text_primary", "text_secondary", "text_muted",
    "input_bg", "input_border",
    "font_display", "font_body", "font_mono",
    "footer_text", "support_url",
]


def branding_to_dict(b: "Branding") -> dict:
    """Serialize branding record to dict for API response."""
    return {
        "app_name": b.app_name,
        "app_subtitle": b.app_subtitle,
        "primary_color": b.primary_color,
        "accent_color": b.accent_color,
        "sidebar_bg": b.sidebar_bg,
        "sidebar_border": b.sidebar_border,
        "sidebar_text": b.sidebar_text,
        "sidebar_active_bg": b.sidebar_active_bg,
        "sidebar_active_text": b.sidebar_active_text,
        "content_bg": b.content_bg,
        "card_bg": b.card_bg,
        "card_border": b.card_border,
        "text_primary": b.text_primary,
        "text_secondary": b.text_secondary,
        "text_muted": b.text_muted,
        "input_bg": b.input_bg,
        "input_border": b.input_border,
        "font_display": b.font_display,
        "font_body": b.font_body,
        "font_mono": b.font_mono,
        "logo_url": b.logo_url,
        "favicon_url": b.favicon_url,
        "footer_text": b.footer_text,
        "support_url": b.support_url,
        "updated_at": str(b.updated_at) if b.updated_at else None,
    }
