"""
modules/notifications/models.py — ORM models for the notifications domain.

Owns tables: alerts, alert_preferences, push_subscriptions
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, Enum as SQLEnum, Text, JSON
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.base import Base, AlertType, AlertSeverity, _ENUM_VALUES


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
