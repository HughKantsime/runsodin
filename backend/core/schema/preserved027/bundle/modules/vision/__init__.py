MODULE_ID = "vision"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Vigil AI print failure detection via ONNX inference on camera frames"

ROUTES = [
    "vision.routes",
]

TABLES = [
    "vision_detections",
    "vision_settings",
    "vision_models",
]

PUBLISHES = [
    "vision.detection",
    "vision.auto_pause",
]

SUBSCRIBES = [
    "printer.state_changed",
]

IMPLEMENTS = []

REQUIRES = ["NotificationDispatcher", "PrinterStateProvider"]

DAEMONS = [
    "vision.monitor",
]


def register(app, registry) -> None:
    """Register the vision module routes."""
    from modules.vision import routes

    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api/v1")
