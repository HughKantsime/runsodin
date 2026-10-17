"""Organizations permissions routes — RBAC page/action access control."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, List
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_role, require_superadmin
from core.models import SystemConfig
# The defaults and the effective map live in core (shared with core.rbac.require_action).
from core.rbac_policy import RBAC_DEFAULT_PAGE_ACCESS, RBAC_DEFAULT_ACTION_ACCESS, get_rbac as _get_rbac

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Endpoints ==============

@router.get("/permissions", tags=["RBAC"])
def get_permissions(db: Session = Depends(get_db)):
    """Get current RBAC permission map. Public (needed at login)."""
    return _get_rbac(db)


class RBACUpdateRequest(PydanticBaseModel):
    model_config = {"strict": True}
    page_access: Dict[str, List[str]]
    action_access: Dict[str, List[str]]


@router.put("/permissions", tags=["RBAC"])
def update_permissions(data: RBACUpdateRequest, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Update RBAC permissions. Superadmin only — system-wide policy."""
    valid_roles = {"admin", "operator", "viewer"}
    for key, roles in data.page_access.items():
        if not isinstance(roles, list):
            raise HTTPException(400, f"page_access.{key} must be a list")
        for r in roles:
            if r not in valid_roles:
                raise HTTPException(400, f"Invalid role '{r}' in page_access.{key}")
        if key in ("admin", "settings", "audit") and "admin" not in roles:
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
    log_audit(db, "permissions_updated", "system", details={"page_keys": list(data.page_access.keys()), "action_keys": list(data.action_access.keys())})
    db.commit()
    return {"message": "Permissions updated", **value}


@router.post("/permissions/reset", tags=["RBAC"])
def reset_permissions(current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Reset permissions to defaults. Superadmin only."""
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row:
        db.delete(row)
    log_audit(db, "permissions_reset", "system", details="Permissions reset to defaults")
    db.commit()
    return {
        "message": "Reset to defaults",
        "page_access": RBAC_DEFAULT_PAGE_ACCESS,
        "action_access": RBAC_DEFAULT_ACTION_ACCESS,
    }
