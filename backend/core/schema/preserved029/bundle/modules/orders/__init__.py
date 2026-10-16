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

REQUIRES = ["AttemptValuationProvider"]

DAEMONS = []


def register_providers(registry) -> None:
    """Install production authority without API routes or database work."""
    from modules.orders.production import OrderFulfillmentService

    if not registry.has_provider("OrderFulfillmentProvider"):
        registry.register_provider("OrderFulfillmentProvider", OrderFulfillmentService())
    provider = registry.get_provider("OrderFulfillmentProvider")
    if not isinstance(provider, OrderFulfillmentService) or any(
        not callable(getattr(provider, method, None))
        for method in (
            "prepare_parent_graph", "recheck_parent_graph", "lock_orders",
            "lock_for_jobs", "assert_job_edit_allowed",
            "assert_order_structure_edit_allowed", "record_item", "bind_job",
            "reconcile_orders", "confirm_item",
        )
    ):
        raise TypeError("Invalid production authority")


def register(app, registry) -> None:
    """Register the orders module providers and routes."""
    register_providers(registry)
    from modules.orders import routes

    app.include_router(routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api/v1")
