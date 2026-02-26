"""
modules/notifications/schemas.py — Pydantic schemas for the notifications domain.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, ConfigDict

from core.base import AlertType, AlertSeverity


# Schema-layer enums (mirror of ORM enums, used in Pydantic validators)
class AlertTypeEnum(str, Enum):
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


class AlertSeverityEnum(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# ============== Alert Schemas ==============

class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    alert_type: AlertTypeEnum
    severity: AlertSeverityEnum
    title: str
    message: Optional[str] = None
    is_read: Optional[bool] = False
    is_dismissed: Optional[bool] = False
    printer_id: Optional[int] = None
    job_id: Optional[int] = None
    spool_id: Optional[int] = None
    metadata_json: Optional[Dict[str, Any]] = None
    created_at: datetime

    # Populated by API for display
    printer_name: Optional[str] = None
    job_name: Optional[str] = None
    spool_name: Optional[str] = None


class AlertSummary(BaseModel):
    """Aggregated alert counts for dashboard widget."""
    print_failed: int = 0
    spool_low: int = 0
    maintenance_overdue: int = 0
    total: int = 0


class AlertPreferenceBase(BaseModel):
    alert_type: AlertTypeEnum
    in_app: bool = True
    browser_push: bool = False
    email: bool = False
    threshold_value: Optional[float] = None


class AlertPreferenceResponse(AlertPreferenceBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int


class AlertPreferencesUpdate(BaseModel):
    """Bulk update of all alert preferences for a user."""
    preferences: List[AlertPreferenceBase]


# ============== Push Subscription Schemas ==============

class PushSubscriptionCreate(BaseModel):
    endpoint: str
    p256dh_key: str
    auth_key: str


# ============== SMTP Config Schemas ==============

class SmtpConfigBase(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    from_address: str = ""
    use_tls: bool = True


class SmtpConfigResponse(BaseModel):
    """SMTP config response (password masked)."""
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    password_set: bool = False
    from_address: str = ""
    use_tls: bool = True
