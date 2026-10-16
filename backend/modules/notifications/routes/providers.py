"""O.D.I.N. — Browser Push Notification Subscriptions."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
import json
import logging

from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.rbac import require_interactive_session, require_superadmin
from modules.notifications.models import PushSubscription
from modules.notifications.schemas import PushSubscriptionCreate

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Alerts"])


# ============== Browser Push Subscription ==============

@router.get("/push/vapid-key")
async def get_vapid_key(db: Session = Depends(get_db)):
    """Get VAPID public key for browser push subscription.

    The key pair is generated locally and persisted on first request, so a
    fresh install can subscribe without any external key service.
    """
    from modules.notifications.vapid import ensure_vapid_keys
    try:
        keys = ensure_vapid_keys(db)
    except Exception as exc:
        db.rollback()
        log.error(f"VAPID keys unavailable: {type(exc).__name__}")
        return {"public_key": None, "enabled": False}
    if not keys.get("public_key"):
        log.error("VAPID_PRIVATE_KEY override set but no public key derivable; set VAPID_PUBLIC_KEY")
        return {"public_key": None, "enabled": False}
    return {"public_key": keys["public_key"], "enabled": True}


@router.post("/push/vapid-key/rotate")
async def rotate_vapid_key(current_user: dict = Depends(require_superadmin(scope="admin")),
                           db: Session = Depends(get_db)):
    """Generate a new VAPID pair and invalidate every push subscription. Superadmin only.

    Refused (409) while the VAPID_PRIVATE_KEY environment override is set.
    Browsers resubscribe automatically when they see the new public key.
    """
    from modules.notifications.vapid import VapidOverrideActive, rotate_vapid_keys
    try:
        result = rotate_vapid_keys(db)
    except VapidOverrideActive as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    log_audit(db, "push.vapid_rotated", "system_config", None,
              {"subscriptions_invalidated": result["subscriptions_invalidated"]})
    db.commit()
    return {"status": "ok", **result}


@router.post("/push/subscribe", dependencies=[Depends(require_interactive_session())])
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
