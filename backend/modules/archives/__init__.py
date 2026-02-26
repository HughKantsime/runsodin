MODULE_ID = "archives"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Print archive history, projects, and timelapse management"

ROUTES = []

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

DAEMONS = []


def register_subscribers(bus) -> None:
    """Register all archives module event subscribers."""
    from modules.archives import archive
    archive.register_subscribers(bus)
