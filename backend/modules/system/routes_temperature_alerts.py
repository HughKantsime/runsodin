"""Temperature-anomaly alert settings (GET/PUT /config/temperature-alerts): platform admin only, audited."""
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_superadmin
from modules.notifications.temperature_alerts import read_settings, store_settings
from modules.notifications.temperature_rules import validate_update

router = APIRouter()


@router.get("/config/temperature-alerts", tags=["Config"])
async def get_temperature_alerts(db: Session = Depends(get_db), current_user: dict = Depends(require_superadmin())):
    """Temperature-anomaly alert settings, defaults filled in."""
    return read_settings(db)


@router.put("/config/temperature-alerts", tags=["Config"])
async def update_temperature_alerts(body: dict = Body(...), current_user: dict = Depends(require_superadmin()),
                                    db: Session = Depends(get_db)):
    """Change any of the settings; unknown or out-of-range values are refused whole."""
    current = read_settings(db)
    try:
        updated = validate_update(body, current)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    changed = {key: value for key, value in updated.items() if current.get(key) != value}
    store_settings(db, updated)
    log_audit(db, "temperature_alerts.updated", "settings", details={"changed": changed})
    db.commit()
    return updated
