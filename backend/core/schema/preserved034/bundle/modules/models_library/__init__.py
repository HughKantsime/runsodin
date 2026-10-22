MODULE_ID = "models_library"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "3D model library, print file management, and .3mf parsing"

ROUTES = [
    "models_library.routes",
]

TABLES = [
    "models",
    "model_revisions",
]

PUBLISHES = []

SUBSCRIBES = []

IMPLEMENTS = []

REQUIRES = []

DAEMONS = []


def register(app, registry) -> None:
    """Register the models_library module routes."""
    from modules.models_library import routes

    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api/v1")
