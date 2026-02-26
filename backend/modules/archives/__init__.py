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
]

IMPLEMENTS = []

REQUIRES = []

DAEMONS = []
