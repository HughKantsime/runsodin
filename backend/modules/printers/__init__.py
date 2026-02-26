MODULE_ID = "printers"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Printer communication, adapters, monitors, and fleet management"

ROUTES = [
    "printers.routes",
    "printers.camera_routes",
]

TABLES = [
    "printers",
    "filament_slots",
    "nozzle_lifecycle",
    "printer_telemetry",
    "hms_error_history",
    "ams_telemetry",
]

PUBLISHES = [
    "printer.state_changed",
    "printer.connected",
    "printer.disconnected",
    "printer.error",
    "printer.hms_code",
]

SUBSCRIBES = [
    "job.completed",
    "job.failed",
]

IMPLEMENTS = ["PrinterStateProvider"]

REQUIRES = ["NotificationDispatcher", "OrgSettingsProvider"]

DAEMONS = [
    "printers.monitors.mqtt_monitor",
    "printers.monitors.moonraker_monitor",
    "printers.monitors.prusalink_monitor",
    "printers.monitors.elegoo_monitor",
]


def register(app, registry) -> None:
    """Register the printers module: routes and PrinterStateProvider."""
    import modules.printers as _self
    from modules.printers import routes, camera_routes

    for router in (routes.router, camera_routes.router):
        app.include_router(router, prefix="/api")
        app.include_router(router, prefix="/api/v1")

    registry.register_provider("PrinterStateProvider", _self)


def register_subscribers(bus) -> None:
    """Register all printers module event subscribers."""
    from modules.printers import smart_plug
    smart_plug.register_subscribers(bus)
