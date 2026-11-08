MODULE_ID = "reporting"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Analytics, scheduled reports, utilization stats, and education reports"

ROUTES = [
    "reporting.routes",
]

TABLES = [
    "report_schedules",
]

PUBLISHES = []

SUBSCRIBES = []

IMPLEMENTS = []

REQUIRES = []

DAEMONS = [
    "reporting.report_runner",
]


def register(app, registry) -> None:
    """Register the reporting module routes."""
    from modules.reporting import routes

    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api/v1")
