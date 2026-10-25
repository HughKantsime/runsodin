MODULE_ID = "archives"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Print archive history, projects, and timelapse management"

ROUTES = [
    "archives.routes",
]

TABLES = [
    "print_archives",
    "filament_delivery_outbox",
    "terminal_filament_measurements",
    "filament_cost_events",
    "projects",
    "timelapses",
    "timelapse_retirements",
    "timelapse_ownership_anchors",
    "timelapse_retirement_objects",
    "timelapse_retirement_parents",
    "timelapse_retirement_quarantine",
    "timelapse_retirement_moves",
    "timelapse_retirement_unlinks",
    "timelapse_media_lifetimes",
    "timelapse_media_versions",
    "timelapse_frame_namespaces",
    "timelapse_frame_bindings",
    "timelapse_publications",
    "timelapse_publication_facts",
    "timelapse_retained_versions",
    "timelapse_retirement_lifetime_bindings",
    "timelapse_retirement_members",
    "timelapse_retirement_container_receipts",
]

PUBLISHES = []

SUBSCRIBES = [
    "job.completed",
    "job.failed",
]

IMPLEMENTS = ["AttemptValuationProvider", "TimelapseMediaProvider"]

REQUIRES = []

DAEMONS = [
    "archives.timelapse_capture",
]


def register_providers(registry) -> None:
    """Validate all present services before registration or API imports; no I/O."""
    from modules.archives.attempt_valuation_provider import AttemptValuationService
    from modules.archives.timelapse_media_provider import TimelapseMediaService
    from core.interfaces.timelapse_media import MEDIA_METHODS
    definitions=(('AttemptValuationProvider',AttemptValuationService,('read_private_valuation','read_order_measured_costs'),'Invalid private valuation service'),
                 ('TimelapseMediaProvider',TimelapseMediaService,MEDIA_METHODS,'Invalid timelapse media service'))
    missing=[]
    for name,factory,methods,message in definitions:
        if not registry.has_provider(name):
            missing.append((name,factory))
            continue
        provider=registry.get_provider(name)
        if not isinstance(provider,factory) or any(not callable(getattr(provider,method,None)) for method in methods):
            raise TypeError(message)
    created=[(name,factory()) for name,factory in missing]
    for name,provider in created:registry.register_provider(name,provider)


def register(app, registry) -> None:
    """Register the archives module routes."""
    register_providers(registry)
    from modules.archives import routes

    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api/v1")


def register_subscribers(bus) -> None:
    """Register all archives module event subscribers."""
    from modules.archives import archive
    archive.register_subscribers(bus)
