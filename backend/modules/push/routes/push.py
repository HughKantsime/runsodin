"""
modules/push/routes/push.py — Push notification device registration and management.

Endpoints:
  POST   /push/register              Register a device token
  DELETE /push/register/{device_id}  Unregister on logout
  GET    /push/preferences           Per-device notification preferences
  PUT    /push/preferences           Update preferences
  POST   /push/live-activity         Register/update Live Activity token
  POST   /push/test                  Send a test push to caller's devices
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user
from modules.push.apns import get_provider
from modules.push.models import PushDevice
from modules.push.schemas import (
    DeviceRegisterRequest,
    DeviceRegisterResponse,
    LiveActivityRequest,
    PushPreferences,
)

log = logging.getLogger("push.routes")
router = APIRouter(tags=["Push Notifications"])


@router.post("/push/register", response_model=DeviceRegisterResponse, status_code=201)
async def register_device(
    body: DeviceRegisterRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Register a native device for push notification delivery.

    Called on every app launch after authentication. If the device is already
    registered, updates the token (APNs tokens rotate on reinstall).
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    user_id = current_user["id"]

    existing = (
        db.query(PushDevice)
        .filter(PushDevice.user_id == user_id, PushDevice.device_id == body.device_id)
        .first()
    )

    if existing:
        existing.token = body.token
        existing.platform = body.platform
        existing.last_seen_at = datetime.now(timezone.utc)
        db.commit()
        log.info(f"Updated push token for device {body.device_id[:8]}… user={user_id}")
    else:
        device = PushDevice(
            user_id=user_id,
            device_id=body.device_id,
            platform=body.platform,
            token=body.token,
        )
        db.add(device)
        db.commit()
        log.info(f"Registered new push device {body.device_id[:8]}… user={user_id}")

    return DeviceRegisterResponse(
        device_id=body.device_id,
        platform=body.platform,
        registered_at=datetime.now(timezone.utc),
    )


@router.delete("/push/register/{device_id}", status_code=204)
async def unregister_device(
    device_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Unregister a device on logout. Silently succeeds if device not found."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    db.query(PushDevice).filter(
        PushDevice.user_id == current_user["id"],
        PushDevice.device_id == device_id,
    ).delete()
    db.commit()


@router.get("/push/preferences")
async def get_preferences(
    device_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get per-device notification preferences."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    device = (
        db.query(PushDevice)
        .filter(
            PushDevice.user_id == current_user["id"],
            PushDevice.device_id == device_id,
        )
        .first()
    )
    if not device:
        raise HTTPException(status_code=404, detail="Device not registered")

    defaults = PushPreferences()
    if device.preferences_json:
        try:
            stored = json.loads(device.preferences_json)
            return {**defaults.model_dump(), **stored}
        except Exception:
            pass
    return defaults.model_dump()


@router.put("/push/preferences", status_code=200)
async def update_preferences(
    device_id: str,
    prefs: PushPreferences,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update per-device notification preferences."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    device = (
        db.query(PushDevice)
        .filter(
            PushDevice.user_id == current_user["id"],
            PushDevice.device_id == device_id,
        )
        .first()
    )
    if not device:
        raise HTTPException(status_code=404, detail="Device not registered")

    device.preferences_json = json.dumps(prefs.model_dump())
    db.commit()
    return {"updated": True}


@router.post("/push/live-activity", status_code=200)
async def update_live_activity(
    body: LiveActivityRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Register or update the APNs Live Activity push token for an active print.

    Called by the iOS client when ActivityKit issues a new push token for a
    Live Activity. The server stores this token and uses it to send background
    pushes with content-state updates as the print progresses.

    action=start: store token and begin sending updates
    action=update: replace token (ActivityKit rotates tokens periodically)
    action=end: clear token (activity dismissed by user)
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Find any registered device for this user to attach the live activity token
    # In practice, the client that starts the activity is the one that registers
    device = (
        db.query(PushDevice)
        .filter(PushDevice.user_id == current_user["id"])
        .order_by(PushDevice.last_seen_at.desc())
        .first()
    )
    if not device:
        raise HTTPException(status_code=404, detail="No registered device found")

    if body.action in ("start", "update"):
        device.live_activity_token = body.activity_token
    elif body.action == "end":
        device.live_activity_token = None

    db.commit()
    return {"action": body.action, "printer_id": body.printer_id}


@router.post("/push/test", status_code=200)
async def send_test_push(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a test push notification to all devices registered to the calling user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    devices = (
        db.query(PushDevice)
        .filter(PushDevice.user_id == current_user["id"])
        .all()
    )
    if not devices:
        return {"sent": 0, "message": "No registered devices"}

    sent = 0
    for device in devices:
        sandbox = device.platform == "apns-sandbox"
        provider = get_provider(sandbox=sandbox)
        ok = provider.send_push(
            device_token=device.token,
            title="ODIN Test Notification",
            body="Push notifications are working correctly.",
            category="TEST",
        )
        if ok:
            sent += 1

    return {"sent": sent, "total_devices": len(devices)}
