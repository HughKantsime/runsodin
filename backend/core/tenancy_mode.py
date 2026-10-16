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


def printer_visible_install_wide(connection, printer_org_id, printer_shared: bool, *, has_organizations=None) -> bool:
    """A printer's content audience is install-wide when it is shared, or when it
    has no organization on an install that has no organizations.

    Pass `has_organizations` when the caller records the same tenancy mode
    elsewhere in one decision, so both use one observation."""
    if printer_shared:
        return True
    if has_organizations is None:
        has_organizations = install_has_organizations(connection)
    return printer_org_id is None and not has_organizations


# 'shared' capture ownership is published media's install-wide audience. The
# capture-time fact (timelapse_capture_tenancy, migration 033, copied to derived
# media) decides it: a capture made while the install had organizations is
# shared because its printer was explicitly shared then (immutable); a capture
# made while the install had none is install-wide only while it still has none.
# Anchors without a row (captured before 033) fall back to the same fail-closed
# rule: install-wide while no organization exists, admin-only once one does.
# The anchor itself stays 'shared' for the hash-pinned publication contract
# (migration 019), which admits only known/shared owners.
SHARED_CAPTURE_AUDIENCE_SQL = (
    "(EXISTS (SELECT 1 FROM timelapse_capture_tenancy capture_tenancy "
    "WHERE capture_tenancy.operation_id=a.operation_id AND capture_tenancy.captured_single_tenant IS FALSE) "
    "OR " + NO_ORGANIZATIONS_SQL + ")")
