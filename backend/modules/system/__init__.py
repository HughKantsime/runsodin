MODULE_ID = "system"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "System config, health, maintenance, backups, admin logs, and slicer profiles"

ROUTES = [
    "system.routes",           # aggregator
    "system.profile_routes",
    "system.routes_health",
    "system.routes_config",
    "system.routes_setup",
    "system.routes_backup",
    "system.routes_settings",
    "system.routes_maintenance",
    "system.routes_admin",
]

TABLES = [
    "maintenance_tasks",
    "maintenance_logs",
    "audit_log",
    "printer_profiles",
]

PUBLISHES = [
    "system.backup_completed",
    "system.license_changed",
]

SUBSCRIBES = []

IMPLEMENTS = []

REQUIRES = []

DAEMONS = []


def register(app, registry) -> None:
    """Register the system module routes."""
    from modules.system import routes, profile_routes

    for router in (routes.router, profile_routes.router):
        app.include_router(router, prefix="/api")
        app.include_router(router, prefix="/api/v1")
