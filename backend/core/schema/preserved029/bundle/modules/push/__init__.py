MODULE_ID = "push"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Native APNs push notifications, Live Activities, and biometric auth tokens"

ROUTES = [
    "push.routes",
]

TABLES = [
    "push_devices",
    "biometric_tokens",
]

PUBLISHES = []

SUBSCRIBES = [
    "job.completed",
    "job.failed",
    "vision.detection",
    "inventory.spool_low",
    "printer.hms_code",
    "printer.disconnected",
]

IMPLEMENTS = []
REQUIRES = []
DAEMONS = []


def register(app, registry) -> None:
    from modules.push import routes
    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api/v1")


def register_subscribers(bus) -> None:
    from modules.push import fanout
    fanout.register_subscribers(bus)
