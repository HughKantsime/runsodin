"""O.D.I.N. — RBAC page/action permission map (system-wide policy).

The defaults and the stored override (system_config key ``rbac_permissions``)
live in core so route modules can enforce an action permission on the server
(``core.rbac.require_action``) without importing the organizations module.
"""

from sqlalchemy.orm import Session

from core.models import SystemConfig


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
    # /audit (frontend RoleGate page="audit"); the audit-log API is superadmin-only.
    "audit": ["admin"],
    "branding": ["admin"],
    "education_reports": ["admin", "operator"],
    "education": ["admin", "operator", "viewer"],
    "orders": ["admin", "operator", "viewer"],
    "products": ["admin", "operator", "viewer"],
    "alerts": ["admin", "operator", "viewer"],
}

RBAC_DEFAULT_ACTION_ACCESS = {
    "settings.edit": ["admin"],
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


def get_rbac(db: Session):
    """Effective permission map: stored overrides on top of the defaults."""
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row and row.value:
        data = row.value
        page = {**RBAC_DEFAULT_PAGE_ACCESS, **data.get("page_access", {})}
        action = {**RBAC_DEFAULT_ACTION_ACCESS, **data.get("action_access", {})}
        return {"page_access": page, "action_access": action}
    return {"page_access": RBAC_DEFAULT_PAGE_ACCESS, "action_access": RBAC_DEFAULT_ACTION_ACCESS}


def action_roles(db: Session, action: str) -> list:
    """Roles granted ``action`` by the effective map (unknown actions grant nobody)."""
    return list(get_rbac(db)["action_access"].get(action) or [])
