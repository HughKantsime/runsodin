MODULE_ID = "archives"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Print archive history, projects, and timelapse management"

ROUTES = [
    "archives.routes",
]

TABLES = [
    "print_archives",
    "projects",
    "timelapses",
]

PUBLISHES = []

SUBSCRIBES = [
    "job.completed",
    "job.failed",
]

IMPLEMENTS = []

REQUIRES = []

DAEMONS = [
    "archives.timelapse_capture",
]


def register(app, registry) -> None:
    """Register the archives module routes."""
    from modules.archives import routes

    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api/v1")


def register_subscribers(bus) -> None:
    """Register all archives module event subscribers."""
    from modules.archives import archive
    archive.register_subscribers(bus)
