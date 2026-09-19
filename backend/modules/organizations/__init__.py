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
    "organizations.routes_education",
    "organizations.routes_education_submissions",
    "organizations.routes_education_reviews",
    "organizations.routes_classroom",
]

TABLES = [
    "groups",
    "users",
    "oidc_config",
    "oidc_pending_states",
    "oidc_auth_codes",
    "quota_usage",
    "education_cost_centers",
    "education_cost_center_grants",
    "education_cost_center_printers",
    "education_submissions",
    "education_upload_operations",
    "education_audit_events",
    "education_notification_outbox",
    "education_rate_counters",
    "education_storage_accounts",
    "classroom_connections",
    "classroom_oauth_states",
    "classroom_course_mappings",
    "classroom_roster_identities",
]

PUBLISHES = []

SUBSCRIBES = []

IMPLEMENTS = ["OrgSettingsProvider", "EducationPolicyProvider"]

REQUIRES = []

DAEMONS = []


def register(app, registry) -> None:
    """Register the organizations module: routes and OrgSettingsProvider."""
    from modules.organizations import (
        auth_routes,
        routes,
        routes_education,
        routes_education_reviews,
        routes_education_submissions,
        routes_classroom,
    )
    from modules.organizations.services import EducationPolicyService, OrgSettingsService

    for router in (
        routes.router,
        auth_routes.router,
        routes_education.router,
        routes_education_submissions.router,
        routes_education_reviews.router,
        routes_classroom.router,
    ):
        app.include_router(router, prefix="/api")
        app.include_router(router, prefix="/api/v1")

    registry.register_provider("OrgSettingsProvider", OrgSettingsService())
    registry.register_provider("EducationPolicyProvider", EducationPolicyService())
