MODULE_ID = "inventory"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Spool and filament inventory, consumables, and drying logs"

ROUTES = []

TABLES = [
    "spools",
    "filament_library",
    "spool_usage",
    "drying_logs",
    "consumables",
    "product_consumables",
    "consumable_usage",
]

PUBLISHES = [
    "inventory.spool_low",
    "inventory.spool_empty",
    "inventory.consumable_low",
]

SUBSCRIBES = []

IMPLEMENTS = []

REQUIRES = []

DAEMONS = []
