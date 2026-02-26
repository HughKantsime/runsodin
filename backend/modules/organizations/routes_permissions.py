"""Organizations permissions routes — RBAC page/action access control."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role
from core.models import SystemConfig

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== RBAC defaults ==============

RBAC_DEFAULT_PAGE_ACCESS = {
    "dashboard": ["admin", "operator", "viewer"],
    "timeline": ["admin", "operator", "viewer"],
    "jobs": ["admin", "operator", "viewer"],
    "printers": ["admin", "operator", "viewer"],
    "models": ["admin", "operator", "viewer"],
    "spools": ["admin", "operator", "viewer"],
    "cameras": ["admin", "operator", "viewer"],
    "analytics": ["admin", "operator", "viewer"],
    "calculator": ["admin", "operator", "viewer"],
    "upload": ["admin", "operator"],
    "maintenance": ["admin", "operator"],
    "settings": ["admin"],
    "admin": ["admin"],
    "branding": ["admin"],
    "education_reports": ["admin", "operator"],
    "orders": ["admin", "operator", "viewer"],
    "products": ["admin", "operator", "viewer"],
    "alerts": ["admin", "operator", "viewer"],
}

RBAC_DEFAULT_ACTION_ACCESS = {
    "jobs.create": ["admin", "operator"],
    "jobs.edit": ["admin", "operator"],
    "jobs.cancel": ["admin", "operator"],
    "jobs.delete": ["admin", "operator"],
    "jobs.start": ["admin", "operator"],
    "jobs.complete": ["admin", "operator"],
    "printers.add": ["admin"],
    "printers.edit": ["admin", "operator"],
    "printers.delete": ["admin"],
    "printers.slots": ["admin", "operator"],
    "printers.reorder": ["admin", "operator"],
    "models.create": ["admin", "operator"],
    "models.edit": ["admin", "operator"],
    "models.delete": ["admin"],
    "spools.edit": ["admin", "operator"],
    "spools.delete": ["admin"],
    "timeline.move": ["admin", "operator"],
    "upload.upload": ["admin", "operator"],
    "upload.schedule": ["admin", "operator"],
    "upload.delete": ["admin", "operator"],
    "maintenance.log": ["admin", "operator"],
    "maintenance.tasks": ["admin"],
    "dashboard.actions": ["admin", "operator"],
    "orders.create": ["admin", "operator"],
    "orders.edit": ["admin"],
    "orders.delete": ["admin", "operator"],
    "orders.ship": ["admin", "operator"],
    "products.create": ["admin", "operator"],
    "products.edit": ["admin", "operator"],
    "products.delete": ["admin"],
    "jobs.approve": ["admin", "operator"],
    "jobs.reject": ["admin", "operator"],
    "jobs.resubmit": ["admin", "operator", "viewer"],
    "alerts.read": ["admin", "operator", "viewer"],
    "printers.plug": ["admin", "operator"],
}


def _get_rbac(db: Session):
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row and row.value:
        data = row.value
        page = {**RBAC_DEFAULT_PAGE_ACCESS, **data.get("page_access", {})}
        action = {**RBAC_DEFAULT_ACTION_ACCESS, **data.get("action_access", {})}
        return {"page_access": page, "action_access": action}
    return {"page_access": RBAC_DEFAULT_PAGE_ACCESS, "action_access": RBAC_DEFAULT_ACTION_ACCESS}


# ============== Endpoints ==============

@router.get("/permissions", tags=["RBAC"])
def get_permissions(db: Session = Depends(get_db)):
    """Get current RBAC permission map. Public (needed at login)."""
    return _get_rbac(db)


class RBACUpdateRequest(PydanticBaseModel):
    page_access: dict
    action_access: dict


@router.put("/permissions", tags=["RBAC"])
def update_permissions(data: RBACUpdateRequest, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update RBAC permissions. Admin only."""
    valid_roles = {"admin", "operator", "viewer"}
    for key, roles in data.page_access.items():
        if not isinstance(roles, list):
            raise HTTPException(400, f"page_access.{key} must be a list")
        for r in roles:
            if r not in valid_roles:
                raise HTTPException(400, f"Invalid role '{r}' in page_access.{key}")
        if key in ("admin", "settings") and "admin" not in roles:
            raise HTTPException(400, f"Cannot remove admin from '{key}' page")

    for key, roles in data.action_access.items():
        if not isinstance(roles, list):
            raise HTTPException(400, f"action_access.{key} must be a list")
        for r in roles:
            if r not in valid_roles:
                raise HTTPException(400, f"Invalid role '{r}' in action_access.{key}")

    value = {"page_access": data.page_access, "action_access": data.action_access}
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row:
        row.value = value
    else:
        row = SystemConfig(key="rbac_permissions", value=value)
        db.add(row)
    db.commit()
    return {"message": "Permissions updated", **value}


@router.post("/permissions/reset", tags=["RBAC"])
def reset_permissions(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Reset permissions to defaults. Admin only."""
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row:
        db.delete(row)
        db.commit()
    return {
        "message": "Reset to defaults",
        "page_access": RBAC_DEFAULT_PAGE_ACCESS,
        "action_access": RBAC_DEFAULT_ACTION_ACCESS,
    }
