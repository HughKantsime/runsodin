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
    "order_production_items",
    "order_production_components",
    "order_production_jobs",
]

PUBLISHES = []

SUBSCRIBES = []

IMPLEMENTS = ["OrderFulfillmentProvider"]

REQUIRES = []

DAEMONS = []


def register(app, registry) -> None:
    """Register the orders module routes."""
    from modules.orders import routes
    from modules.orders.production import OrderFulfillmentService
    registry.register_provider("OrderFulfillmentProvider", OrderFulfillmentService())

    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api/v1")
