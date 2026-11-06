MODULE_ID = "organizations"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Organizations, users, roles, OIDC/SSO, branding, and quota management"

ROUTES = [
    "organizations.routes",
    "organizations.auth_routes",       # aggregator
    "organizations.routes_auth",
    "organizations.routes_mfa_enrollment",
    "organizations.routes_oidc",
    "organizations.routes_sessions",
    "organizations.routes_users",
    "organizations.routes_permissions",
    "organizations.routes_education",
    "organizations.routes_education_training",
    "organizations.routes_education_classes",
    "organizations.routes_education_program_reports",
    "organizations.routes_education_roster",
    "organizations.routes_roster_auth",
    "organizations.routes_term_retention",
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
    "mfa_enrollments",
    "quota_usage",
    "education_term_policies",
    "education_term_cleanup",
    "education_cost_centers",
    "education_trainings",
    "education_training_records",
    "education_classes",
    "education_program_marks",
    "education_instructor_hours",
    "education_roster_connections",
    "education_roster_courses",
    "education_roster_identities",
    "education_roster_receipts",
    "education_assignments",
    "education_assignment_submissions",
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
        routes_education_training,
        routes_education_classes,
        routes_education_program_reports,
        routes_education_roster,
        routes_roster_auth,
        routes_term_retention,
        routes_education_reviews,
        routes_education_submissions,
        routes_classroom,
    )

    from modules.organizations.education_slice_info import router as education_slice_router

    for router in (
        routes.router,
        auth_routes.router,
        routes_education.router,
        routes_education_training.router,
        routes_education_classes.router,
        routes_education_program_reports.router,
        routes_education_roster.router,
        routes_roster_auth.router,
        routes_term_retention.router,
        education_slice_router,
        routes_education_submissions.router,
        routes_education_reviews.router,
        routes_classroom.router,
    ):
        app.include_router(router, prefix="/api")
        app.include_router(router, prefix="/api/v1")

    register_providers(registry)


def register_providers(registry) -> None:
    """Register process-local services without constructing an API application."""
    from modules.organizations.services import EducationPolicyService, OrgSettingsService

    for name, factory in (("OrgSettingsProvider", OrgSettingsService),
                          ("EducationPolicyProvider", EducationPolicyService)):
        if not registry.has_provider(name):
            registry.register_provider(name, factory())
