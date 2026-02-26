MODULE_ID = "organizations"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Organizations, users, roles, OIDC/SSO, branding, and quota management"

ROUTES = [
    "organizations.routes",
    "organizations.auth_routes",
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
    import modules.organizations as _self
    from modules.organizations import routes, auth_routes

    for router in (routes.router, auth_routes.router):
        app.include_router(router, prefix="/api")
        app.include_router(router, prefix="/api/v1")

    registry.register_provider("OrgSettingsProvider", _self)
