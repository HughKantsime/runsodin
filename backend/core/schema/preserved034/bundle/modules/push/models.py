"""
modules/push/models.py — ORM models for native push notifications.

Owns tables: push_devices, biometric_tokens
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, UniqueConstraint
from sqlalchemy.sql import func

from core.base import Base


class PushDevice(Base):
    """
    Registered native device for push notification delivery.

    One row per physical device per user. A user may have multiple devices
    (iPhone + iPad + Mac). Device tokens are platform-specific and rotate
    on OS reinstall.

    platform values:
      apns         — iOS/iPadOS/macOS production APNs
      apns-sandbox — iOS/iPadOS/macOS development APNs (Xcode builds)
    """
    __tablename__ = "push_devices"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)

    # Client-generated UUID — stable identifier for this device across token rotations
    device_id = Column(String(36), nullable=False, index=True)

    # apns | apns-sandbox
    platform = Column(String(20), nullable=False, default="apns")

    # APNs device token (hex string, 64 chars)
    token = Column(Text, nullable=False)

    # ActivityKit push-to-start/update token — set per active print job
    live_activity_token = Column(Text, nullable=True)

    # JSON: {quiet_hours: {start: "22:00", end: "08:00"}, categories: {SPOOL_LOW: false, ...}}
    preferences_json = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    last_seen_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # One registration per device per user — re-registering updates the token
        UniqueConstraint("user_id", "device_id", name="uq_push_device_user_device"),
    )

    def __repr__(self):
        return f"<PushDevice {self.device_id} user={self.user_id} platform={self.platform}>"


class BiometricToken(Base):
    """
    Long-lived refresh token for Face ID / Touch ID silent re-authentication.

    Exchange flow:
      1. User logs in with password → receives JWT (24h)
      2. Client calls POST /auth/biometric-token → receives BiometricToken (30 days)
      3. On subsequent app opens: Face ID gate → POST /auth/biometric-refresh → new JWT
      4. On logout: DELETE /auth/biometric-token → token revoked

    Tokens are rolling: each /biometric-refresh issues a new token and invalidates
    the previous one (single-use-on-refresh).
    """
    __tablename__ = "biometric_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)

    # Client-generated UUID identifying the device this token belongs to
    device_id = Column(String(36), nullable=False, index=True)

    # SHA-256 hash of the token value (never store plaintext)
    token_hash = Column(String(64), nullable=False, unique=True)

    # Token has been consumed by a refresh — one-time use per rotation
    is_revoked = Column(Boolean, default=False, nullable=False)

    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    last_used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="uq_biometric_user_device"),
    )

    def __repr__(self):
        return f"<BiometricToken user={self.user_id} device={self.device_id} revoked={self.is_revoked}>"
