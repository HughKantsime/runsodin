"""
core/models.py — Core/system ORM models.

Owns tables: system_config, audit_logs
"""

from datetime import datetime
from sqlalchemy import Column, String, DateTime, Integer, JSON
from sqlalchemy.sql import func

from core.base import Base


class SystemConfig(Base):
    """Key-value config store for system settings (RBAC, etc.)."""
    __tablename__ = "system_config"

    key = Column(String(100), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


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
