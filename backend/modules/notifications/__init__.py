MODULE_ID = "notifications"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Alert dispatch, push notifications, webhooks, and quiet hours"

ROUTES = []

TABLES = [
    "alerts",
    "alert_preferences",
    "push_subscriptions",
    "webhooks",
]

PUBLISHES = []

SUBSCRIBES = [
    "printer.state_changed",
    "printer.connected",
    "printer.disconnected",
    "printer.error",
    "printer.hms_code",
    "job.completed",
    "job.failed",
    "vision.detection",
    "vision.auto_pause",
    "inventory.spool_low",
    "inventory.spool_empty",
    "inventory.consumable_low",
    "system.backup_completed",
]

IMPLEMENTS = ["NotificationDispatcher"]

REQUIRES = ["OrgSettingsProvider"]

DAEMONS = []
