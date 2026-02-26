MODULE_ID = "jobs"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Job scheduling, print queue, and timeline management"

ROUTES = []

TABLES = [
    "jobs",
    "scheduler_runs",
    "print_presets",
    "print_jobs",
    "print_files",
]

PUBLISHES = [
    "job.created",
    "job.started",
    "job.completed",
    "job.failed",
    "job.cancelled",
]

SUBSCRIBES = [
    "printer.state_changed",
]

IMPLEMENTS = ["JobStateProvider"]

REQUIRES = ["PrinterStateProvider"]

DAEMONS = []
