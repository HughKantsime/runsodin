"""Install tenancy mode: single-tenant (no organizations) or multi-tenant.

An install with no organizations is one tenant: every resource without an
organization belongs to the install. Ownership that cannot be resolved to an
organization therefore resolves to the install, and the install's users see
it according to their role. As soon as one organization exists, unresolved
ownership is fail-closed again (platform admins only).
"""
from sqlalchemy import text

# Usable inside a larger WHERE clause; true while the install has no organizations.
NO_ORGANIZATIONS_SQL = "NOT EXISTS (SELECT 1 FROM groups WHERE is_org IS TRUE)"


def install_has_organizations(connection) -> bool:
    """`connection` is a SQLAlchemy Session or Connection."""
    return connection.execute(text("SELECT 1 FROM groups WHERE is_org IS TRUE LIMIT 1")).first() is not None


def printer_visible_install_wide(connection, printer_org_id, printer_shared: bool) -> bool:
    """A printer's content audience is install-wide when it is shared, or when it
    has no organization on an install that has no organizations."""
    if printer_shared:
        return True
    return printer_org_id is None and not install_has_organizations(connection)
