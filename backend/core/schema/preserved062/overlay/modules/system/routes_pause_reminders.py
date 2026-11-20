"""Farm-wide pause reminder settings, with audited administrator changes."""
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_role, require_action
from modules.notifications.services import (
    read_pause_reminder_settings as read_settings,
    store_pause_reminder_settings as store_settings,
    validate_pause_reminder_update as validate_update,
)

router = APIRouter()


@router.get("/config/pause-reminders", tags=["Config"])
async def get_pause_reminders(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """Pause reminder settings, defaults filled in."""
    return read_settings(db)


@router.put("/config/pause-reminders", tags=["Config"])
async def update_pause_reminders(body: dict = Body(...), current_user: dict = Depends(require_action("settings.edit")),
                                    db: Session = Depends(get_db)):
    """Change any of the settings; unknown or out-of-range values are refused whole."""
    current = read_settings(db)
    try:
        updated = validate_update(body, current)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    changed = {key: value for key, value in updated.items() if current.get(key) != value}
    store_settings(db, updated)
    log_audit(db, "pause_reminders.updated", "settings", details={"changed": changed})
    db.commit()
    return updated
