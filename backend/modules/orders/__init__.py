MODULE_ID = "orders"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Orders, products, BOMs, invoicing, and cost tracking"

ROUTES = [
    "orders.routes",
]

TABLES = [
    "orders",
    "order_items",
    "products",
    "product_components",
]

PUBLISHES = []

SUBSCRIBES = []

IMPLEMENTS = []

REQUIRES = []

DAEMONS = []


def register(app, registry) -> None:
    """Register the orders module routes."""
    from modules.orders import routes

    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api/v1")
