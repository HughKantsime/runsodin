MODULE_ID = "jobs"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Job scheduling, print queue, and timeline management"

ROUTES = [
    "jobs.routes",
    "jobs.scheduler_routes",
]

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

IMPLEMENTS = ["JobStateProvider", "QueueCreationProvider"]

REQUIRES = ["PrinterStateProvider"]

DAEMONS = []



def register_providers(registry) -> None:
    """Install queue staging without importing API routes or opening a DB."""
    from modules.jobs.queue_creation_provider import QueueCreationService
    if not registry.has_provider("QueueCreationProvider"):
        registry.register_provider("QueueCreationProvider", QueueCreationService())
    provider = registry.get_provider("QueueCreationProvider")
    if not isinstance(provider, QueueCreationService) or any(
        not callable(getattr(provider, method, None)) for method in ("stage_job", "stage_values")
    ):
        raise TypeError("Invalid queue creation service")

def register(app, registry) -> None:
    """Register the jobs module: routes and JobStateProvider."""
    register_providers(registry)
    import modules.jobs as _self
    from modules.jobs import routes, scheduler_routes

    for router in (routes.router, scheduler_routes.router):
        app.include_router(router, prefix="/api")
        app.include_router(router, prefix="/api/v1")

    registry.register_provider("JobStateProvider", _self)
