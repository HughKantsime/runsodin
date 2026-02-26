MODULE_ID = "system"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "System config, health, maintenance, backups, admin logs, and slicer profiles"

ROUTES = []

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
