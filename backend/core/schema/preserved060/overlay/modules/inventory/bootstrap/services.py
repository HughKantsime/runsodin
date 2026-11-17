"""Schema-time inventory services; keep this boundary free of auth and RBAC."""


def backfill_catalogue_materials(connection):
    """Startup repair of newly recognized catalogue materials, in the caller's transaction."""
    from modules.inventory.catalogue import backfill_catalogue_materials as backfill
    backfill(connection)
