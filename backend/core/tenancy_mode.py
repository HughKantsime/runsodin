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


# 'shared' capture ownership is published media's install-wide audience. Captures
# made while the install had organizations are shared because their printer was
# explicitly shared then (immutable, as before). Captures made while the install
# had none are 'shared' only because every org-less printer was install-wide; they
# stay install-wide while the install has no organizations or the printer is shared
# now, and are withdrawn (fail-closed) once the first organization exists. The
# anchor must stay 'shared' for the hash-pinned publication contract (migration
# 019), which admits only known/shared owners, so the audience is decided here,
# at read time, from the anchor's capture time and the organizations' creation.
SHARED_CAPTURE_AUDIENCE_SQL = (
    "(EXISTS (SELECT 1 FROM groups capture_org WHERE capture_org.is_org IS TRUE "
    "AND capture_org.created_at <= a.created_at) "
    "OR EXISTS (SELECT 1 FROM printers capture_printer WHERE capture_printer.id=a.printer_id "
    "AND capture_printer.shared IS TRUE) OR " + NO_ORGANIZATIONS_SQL + ")")
