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
    "printers.dispatch_recovery": ["admin"],
    "settings.edit": ["admin"],
    "users.restore_superadmin": ["admin"],
    # Education grants still decide which centers a principal may reorder.
    "education.reorder": ["admin", "operator", "viewer"],
    # Submitting a job (POST /jobs, schedule from a model): every role. Viewer
    # submissions wait for approval when the approval workflow is on.
    "jobs.submit": ["admin", "operator", "viewer"],
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
    # Operators archive spools (certified claim F06-6.1-03; the server always allowed it).
    "spools.delete": ["admin", "operator"],
    "timeline.move": ["admin", "operator"],
    "models.plates": ["admin", "operator"],
    "upload.upload": ["admin", "operator"],
    "upload.schedule": ["admin", "operator"],
    "upload.delete": ["admin", "operator"],
    "maintenance.log": ["admin", "operator"],
    "maintenance.tasks": ["admin", "operator"],
    "orders.create": ["admin", "operator"],
    "orders.edit": ["admin"],
    "orders.delete": ["admin", "operator"],
    "orders.ship": ["admin", "operator"],
    "products.operations": ["admin", "operator"],
    "products.materials_manage": ["admin"],
    "products.materials_preview": ["admin", "operator"],
    "products.materials_receive": ["admin", "operator"],
    "products.finishing": ["admin", "operator"],
    "products.assortment": ["admin"],
    "products.assortment_preview": ["admin", "operator"],
    "products.square_manage": ["admin"],
    "products.square_send": ["admin", "operator"],
    "products.recipes": ["admin"],
    "products.inspections": ["admin", "operator"],
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
        # Stored overrides apply only to keys that exist (a retired key such as
        # dashboard.actions is dropped instead of lingering in the matrix).
        stored_pages, stored_actions = data.get("page_access", {}), data.get("action_access", {})
        page = {key: stored_pages.get(key, roles) for key, roles in RBAC_DEFAULT_PAGE_ACCESS.items()}
        action = {key: stored_actions.get(key, roles) for key, roles in RBAC_DEFAULT_ACTION_ACCESS.items()}
        return {"page_access": page, "action_access": action}
    return {"page_access": RBAC_DEFAULT_PAGE_ACCESS, "action_access": RBAC_DEFAULT_ACTION_ACCESS}


def action_roles(db: Session, action: str) -> list:
    """Roles granted ``action`` by the effective map (unknown actions grant nobody)."""
    return list(get_rbac(db)["action_access"].get(action) or [])


def page_roles(db: Session, page: str) -> list:
    """Roles granted ``page`` by the effective map (unknown pages grant nobody)."""
    return list(get_rbac(db)["page_access"].get(page) or [])
