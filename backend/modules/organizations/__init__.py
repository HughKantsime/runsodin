MODULE_ID = "organizations"
MODULE_VERSION = "1.0.0"
MODULE_DESCRIPTION = "Organizations, users, roles, OIDC/SSO, branding, and quota management"

ROUTES = []

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
