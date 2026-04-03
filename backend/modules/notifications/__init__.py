MODULE_ID = "notifications"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Alert dispatch, push notifications, webhooks, and quiet hours"

ROUTES = [
    "notifications.routes",
]

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


def register(app, registry) -> None:
    """Register the notifications module: routes and NotificationDispatcher."""
    import modules.notifications as _self
    from modules.notifications import routes

    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api/v1")

    registry.register_provider("NotificationDispatcher", _self)


def register_subscribers(bus) -> None:
    """Register all notifications module event subscribers."""
    from modules.notifications import mqtt_republish
    mqtt_republish.register_subscribers(bus)
