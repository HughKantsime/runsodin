"""O.D.I.N. — Browser Push Notification Subscriptions."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
import json
import logging

from core.db import get_db
from core.dependencies import get_current_user
from modules.notifications.models import PushSubscription
from modules.notifications.schemas import PushSubscriptionCreate

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Alerts"])


# ============== Browser Push Subscription ==============

@router.get("/push/vapid-key")
async def get_vapid_key(db: Session = Depends(get_db)):
    """Get VAPID public key for browser push subscription."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'vapid_keys'")).fetchone()
    if not row:
        return {"public_key": None, "enabled": False}
    try:
        keys = json.loads(row[0])
        return {"public_key": keys.get("public_key"), "enabled": True}
    except Exception:
        return {"public_key": None, "enabled": False}


@router.post("/push/subscribe")
async def subscribe_push(
    data: PushSubscriptionCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Store a browser push subscription for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    existing = db.query(PushSubscription).filter(
        PushSubscription.user_id == current_user["id"],
        PushSubscription.endpoint == data.endpoint
    ).first()

    if existing:
        existing.p256dh_key = data.p256dh_key
        existing.auth_key = data.auth_key
    else:
        db.add(PushSubscription(
            user_id=current_user["id"],
            endpoint=data.endpoint,
            p256dh_key=data.p256dh_key,
            auth_key=data.auth_key
        ))

    db.commit()
    return {"status": "ok", "message": "Push subscription registered"}


@router.delete("/push/subscribe")
async def unsubscribe_push(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remove all push subscriptions for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.query(PushSubscription).filter(
        PushSubscription.user_id == current_user["id"]
    ).delete()
    db.commit()
    return {"status": "ok", "message": "Push subscriptions removed"}
