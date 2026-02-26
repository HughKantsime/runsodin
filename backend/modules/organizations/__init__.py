MODULE_ID = "organizations"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Organizations, users, roles, OIDC/SSO, branding, and quota management"

ROUTES = [
    "organizations.routes",
    "organizations.auth_routes",       # aggregator
    "organizations.routes_auth",
    "organizations.routes_oidc",
    "organizations.routes_sessions",
    "organizations.routes_users",
    "organizations.routes_permissions",
]

TABLES = [
    "groups",
    "users",
    "oidc_config",
    "oidc_pending_states",
    "oidc_auth_codes",
    "quota_usage",
]

PUBLISHES = []

SUBSCRIBES = []

IMPLEMENTS = ["OrgSettingsProvider"]

REQUIRES = []

DAEMONS = []


def register(app, registry) -> None:
    """Register the organizations module: routes and OrgSettingsProvider."""
    from modules.organizations import routes, auth_routes
    from modules.organizations.services import OrgSettingsService

    for router in (routes.router, auth_routes.router):
        app.include_router(router, prefix="/api")
        app.include_router(router, prefix="/api/v1")

    registry.register_provider("OrgSettingsProvider", OrgSettingsService())
