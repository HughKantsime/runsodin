"""
modules/push/schemas.py — Pydantic request/response schemas for push module.
"""

from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class DeviceRegisterRequest(BaseModel):
    """Body for POST /api/v1/push/register"""
    device_id: str = Field(..., description="Client-generated UUID, stable across token rotations")
    platform: str = Field("apns", pattern="^(apns|apns-sandbox)$")
    token: str = Field(..., min_length=10, description="APNs device token (hex string)")


class DeviceRegisterResponse(BaseModel):
    device_id: str
    platform: str
    registered_at: datetime


class PushPreferences(BaseModel):
    """Per-device notification preferences."""
    quiet_hours_enabled: bool = False
    quiet_hours_start: str = "22:00"   # HH:MM local time
    quiet_hours_end: str = "08:00"     # HH:MM local time
    # Per-category toggles — True = send push, False = suppress
    categories: Dict[str, bool] = Field(default_factory=lambda: {
        "PRINT_COMPLETE": True,
        "PRINT_FAILED": True,
        "SPAGHETTI_DETECTED": True,
        "FIRST_LAYER_ISSUE": True,
        "SPOOL_LOW": True,
        "JOB_APPROVAL_REQUIRED": True,
        "HMS_ERROR": True,
        "PRINTER_OFFLINE": True,
    })


class LiveActivityRequest(BaseModel):
    """Body for POST /api/v1/push/live-activity"""
    printer_id: int
    activity_token: str = Field(..., description="APNs Live Activity push token from ActivityKit")
    action: str = Field(..., pattern="^(start|update|end)$")


class BiometricTokenResponse(BaseModel):
    """Response for POST /api/v1/auth/biometric-token"""
    refresh_token: str
    expires_at: datetime
    device_id: str


class BiometricRefreshRequest(BaseModel):
    """Body for POST /api/v1/auth/biometric-refresh"""
    refresh_token: str
    device_id: str


class BiometricRefreshResponse(BaseModel):
    """New JWT issued from a biometric refresh token"""
    access_token: str
    token_type: str = "bearer"
