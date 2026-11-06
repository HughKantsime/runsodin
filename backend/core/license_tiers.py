"""Canonical license tier capabilities."""
# ── Tier definitions ──
TIERS = {
    "community": {
        "name": "Community",
        "max_printers": 5,
        "max_users": 1,
        # Browser push is included in every tier (it is listed in each tier below too).
        "features": ["push_notifications"],
    },
    "pro": {
        "name": "Pro",
        "max_printers": None,
        "max_users": None,
        "features": [
            "multi_user", "rbac", "sso", "white_label",
            "permissions", "branding",
            "webhooks", "email_notifications", "push_notifications",
            "orders", "products", "bom",
            "analytics", "csv_export", "cost_calculator",
            "maintenance", "care_counters",
            "usage_reports",
        ],
    },
    "education": {
        "name": "Education",
        "max_printers": None,
        "max_users": None,
        "features": [
            "multi_user", "rbac", "sso", "white_label",
            "permissions", "branding",
            "webhooks", "email_notifications", "push_notifications",
            "orders", "products", "bom",
            "analytics", "csv_export", "cost_calculator",
            "maintenance", "care_counters",
            "job_approval", "user_groups", "print_quotas", "usage_reports",
            "education_workflows",
        ],
    },
    "enterprise": {
        "name": "Enterprise",
        "max_printers": None,
        "max_users": None,
        "features": [
            "multi_user", "rbac", "sso", "white_label",
            "permissions", "branding",
            "webhooks", "email_notifications", "push_notifications",
            "orders", "products", "bom",
            "analytics", "csv_export", "cost_calculator",
            "maintenance", "care_counters",
            "job_approval", "user_groups", "print_quotas", "usage_reports",
            "education_workflows",
            "mqtt_republish", "audit_export", "audit_integrity", "siem_stream",
        ],
    },
}

