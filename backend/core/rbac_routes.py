"""O.D.I.N. — which API routes each Settings > Permissions key governs.

This module is the source of truth for server-side enforcement of the role
permission matrix (core.rbac_policy). Every action key maps to the routes that
perform that action; each of those routes declares
``core.rbac.require_action(<key>)``, and every page key that has page-only
read endpoints maps to them (``core.rbac.require_page(<page>)``).
tests/test_contracts/test_rbac_matrix_enforcement.py checks the map against
the live route table: every key is mapped, every mapped route carries its
dependency, and every write route under /api is either mapped or listed in
WRITE_EXEMPTIONS with a reason.

Rules the dependencies apply (core.rbac):
* the caller's role must be listed for the key in the effective matrix
  (stored overrides on top of the defaults), read on every request;
* the platform superadmin (admin with no organization) always passes, so the
  install owner cannot be locked out;
* API tokens are checked against the owning user's role, and their scopes
  still apply;
* background jobs, monitors and the CLI have no request principal and are
  not affected.

Paths are the /api paths; the /api/v1 aliases share the same route handlers.
"""

ACTION_ROUTES: dict[str, list[tuple[str, str]]] = {
    "printers.dispatch_recovery": [("POST", "/api/printers/{printer_id}/dispatch-recovery/" + action) for action in ("hold", "reconcile-retain", "release")],
    "users.restore_superadmin": [("POST", "/api/users/{user_id}/restore-superadmin")],
    "education.reorder": [("PATCH", "/api/education/queues/jobs/{printer_id}/order")],
    "products.square_manage": [
        ("PUT", "/api/products/square/activation"),
        ("PUT", "/api/products/{product_id}/square/baseline"),
        ("POST", "/api/products/batches/additions/{addition_id}/baseline-review"),
        ("PUT", "/api/products/square"),
        ("DELETE", "/api/products/square"),
        ("PUT", "/api/products/{product_id}/square"),
        ("DELETE", "/api/products/{product_id}/square"),
    ],
    "products.square_send": [
        ("POST", "/api/products/batches/additions/{addition_id}/send"),
    ],
    "products.recipes": [
        ("POST", "/api/products/production/recipes"),
    ],
    "products.inspections": [
        ("POST", "/api/products/production/inspections"),
        ("POST", "/api/products/production/additions/{addition_id}/check"),
    ],
    "products.operations": [
        ("PUT", "/api/products/production/operations/kit"),
        ("PUT", "/api/products/production/operations/events"),
        ("PUT", "/api/products/production/operations/events/{event_id}/packing/{item_id}"),
        ("PUT", "/api/products/production/operations/capacity"),
    ],
    "products.materials_manage": [
        ("POST", "/api/consumables/purchases/materials"),
        ("POST", "/api/consumables/purchases/adopt"),
        ("POST", "/api/consumables/purchases/mappings"),
    ],
    "products.materials_preview": [
        ("POST", "/api/consumables/purchases/preview"),
        ("POST", "/api/consumables/purchases/email/preview"),
    ],
    "products.materials_receive": [
        ("POST", "/api/consumables/purchases/lines"),
        ("POST", "/api/consumables/purchases/receipts"),
    ],
    "products.finishing": [
        ("POST", "/api/products/production/components"),
        ("POST", "/api/products/production/finishing"),
    ],
    "products.assortment": [
        ("PUT", "/api/products/production/assortment/{product_id}"),
    ],
    "products.assortment_preview": [
        ("POST", "/api/products/production/assortment/{product_id}/preview"),
    ],
    "settings.edit": [
        ("PUT", "/api/config/require-job-approval"),
        ("PUT", "/api/config/require-mfa"),
        ("PUT", "/api/settings/energy-rate"),
        ("PUT", "/api/pricing-config"),
        ("PUT", "/api/orders/finance-settings"),
        ("POST", "/api/setup/network"),
        ("PATCH", "/api/printers/{printer_id}/vision"),
    ],
    "jobs.submit": [
        ("POST", "/api/jobs"),
        ("POST", "/api/models/{model_id}/schedule"),
    ],
    "jobs.create": [
        ("POST", "/api/jobs/batch"),
        ("POST", "/api/jobs/bulk"),
        ("POST", "/api/jobs/{job_id}/repeat"),
        ("POST", "/api/presets"),
        ("POST", "/api/presets/{preset_id}/schedule"),
        ("POST", "/api/scheduler/run"),
        ("POST", "/api/prints"),
        ("POST", "/api/archives/{archive_id}/reprint"),
    ],
    "jobs.edit": [
        ("PATCH", "/api/jobs/{job_id}"),
        ("POST", "/api/jobs/bulk-update"),
        ("PATCH", "/api/jobs/reorder"),
        ("PATCH", "/api/jobs/{job_id}/failure"),
        ("POST", "/api/jobs/{job_id}/link-print"),
        ("POST", "/api/jobs/{job_id}/reset"),
    ],
    "jobs.cancel": [("POST", "/api/jobs/{job_id}/cancel")],
    "jobs.delete": [
        ("DELETE", "/api/jobs/{job_id}"),
        ("DELETE", "/api/presets/{preset_id}"),
    ],
    "jobs.start": [
        ("POST", "/api/jobs/{job_id}/start"),
        ("POST", "/api/jobs/{job_id}/dispatch"),
    ],
    "jobs.complete": [
        ("POST", "/api/jobs/{job_id}/complete"),
        ("POST", "/api/jobs/{job_id}/fail"),
    ],
    "jobs.approve": [("POST", "/api/jobs/{job_id}/approve")],
    "jobs.reject": [("POST", "/api/jobs/{job_id}/reject")],
    "jobs.resubmit": [("POST", "/api/jobs/{job_id}/resubmit")],
    "printers.add": [("POST", "/api/printers")],
    "printers.edit": [
        ("PATCH", "/api/printers/{printer_id}"),
        ("POST", "/api/printers/test-connection"),
        ("POST", "/api/bambu/test-connection"),
        ("PATCH", "/api/cameras/{printer_id}/toggle"),
        ("POST", "/api/printers/{printer_id}/clear-errors"),
        ("POST", "/api/printers/{printer_id}/fan"),
        ("POST", "/api/printers/{printer_id}/lights"),
        ("POST", "/api/printers/{printer_id}/pause"),
        ("POST", "/api/printers/{printer_id}/resume"),
        ("POST", "/api/printers/{printer_id}/stop"),
        ("POST", "/api/printers/{printer_id}/speed"),
        ("POST", "/api/printers/{printer_id}/skip-objects"),
        ("POST", "/api/profiles/{profile_id}/apply"),
        ("POST", "/api/printers/{printer_id}/plate-cleared"),
        ("POST", "/api/printers/{printer_id}/ams/refresh"),
        ("PUT", "/api/printers/{printer_id}/ams/{ams_id}/slots/{slot_id}"),
        ("POST", "/api/printers/{printer_id}/nozzle"),
        ("PATCH", "/api/printers/{printer_id}/nozzle/{nozzle_id}/retire"),
        ("POST", "/api/printers/{printer_id}/sync-ams"),
        ("POST", "/api/printers/{printer_id}/bambu/sync-ams"),
        ("PUT", "/api/printers/{printer_id}/plug"),
        ("DELETE", "/api/printers/{printer_id}/plug"),
    ],
    "printers.delete": [("DELETE", "/api/printers/{printer_id}")],
    "printers.slots": [
        ("PATCH", "/api/printers/{printer_id}/slots/{slot_number}"),
        ("POST", "/api/printers/{printer_id}/slots/{slot_number}/assign"),
        ("POST", "/api/printers/{printer_id}/slots/{slot_number}/confirm"),
        ("PATCH", "/api/printers/{printer_id}/slots/{slot_number}/manual-assign"),
        ("POST", "/api/filament-slots"),
    ],
    "printers.reorder": [("POST", "/api/printers/reorder")],
    "printers.plug": [
        ("POST", "/api/printers/{printer_id}/plug/on"),
        ("POST", "/api/printers/{printer_id}/plug/off"),
        ("POST", "/api/printers/{printer_id}/plug/toggle"),
    ],
    "models.create": [
        ("POST", "/api/models"),
        ("POST", "/api/models/{model_id}/revisions"),
    ],
    "models.edit": [
        ("POST", "/api/models/{model_id}/hide"),
        ("PATCH", "/api/models/{model_id}"),
        ("POST", "/api/models/{model_id}/revisions/{rev_number}/revert"),
    ],
    "models.delete": [
        ("DELETE", "/api/models/{model_id}"),
        ("DELETE", "/api/models/{model_id}/variants/{variant_id}"),
    ],
    "spools.edit": [
        ("POST", "/api/spools/receive"),
        ("PUT", "/api/spools/reorder-policy"),
        ("POST", "/api/spools"),
        ("POST", "/api/spools/bulk-update"),
        ("POST", "/api/spools/scan-assign"),
        ("PATCH", "/api/spools/{spool_id}"),
        ("POST", "/api/spools/{spool_id}/dry"),
        ("POST", "/api/spools/{spool_id}/load"),
        ("POST", "/api/spools/{spool_id}/unload"),
        ("PATCH", "/api/spools/{spool_id}/use"),
        ("POST", "/api/spools/{spool_id}/use"),
        ("POST", "/api/spools/{spool_id}/weigh"),
        ("POST", "/api/filaments"),
        ("PATCH", "/api/filaments/{filament_id}"),
        ("DELETE", "/api/filaments/{filament_id}"),
        ("POST", "/api/spoolman/sync"),
    ],
    "spools.delete": [("DELETE", "/api/spools/{spool_id}")],
    "timeline.move": [("PATCH", "/api/jobs/{job_id}/move")],
    "models.plates": [("GET", "/api/models/{model_id}/plates")],
    "upload.upload": [("POST", "/api/print-files/upload")],
    "upload.schedule": [("POST", "/api/print-files/{file_id}/schedule")],
    "upload.delete": [("DELETE", "/api/print-files/{file_id}")],
    "maintenance.log": [("POST", "/api/maintenance/logs")],
    "maintenance.tasks": [
        ("POST", "/api/maintenance/tasks"),
        ("PATCH", "/api/maintenance/tasks/{task_id}"),
    ],
    "orders.create": [
        ("POST", "/api/orders"),
        ("POST", "/api/orders/{order_id}/items"),
        ("POST", "/api/orders/{order_id}/schedule"),
    ],
    "orders.edit": [
        ("PUT", "/api/orders/{order_id}/show-fulfillment"),
        ("PUT", "/api/orders/{order_id}/show-fulfillment/shipping"),
        ("PATCH", "/api/orders/{order_id}"),
        ("PATCH", "/api/orders/{order_id}/items/{item_id}"),
    ],
    "orders.delete": [
        ("DELETE", "/api/orders/{order_id}"),
        ("DELETE", "/api/orders/{order_id}/items/{item_id}"),
    ],
    "orders.ship": [
        ("POST", "/api/orders/{order_id}/show-fulfillment/dispatch"),
        ("PATCH", "/api/orders/{order_id}/ship"),
        ("POST", "/api/orders/{order_id}/items/{item_id}/confirm-production"),
    ],
    "products.create": [
        ("POST", "/api/products"),
        ("POST", "/api/products/{product_id}/components"),
        ("POST", "/api/products/{product_id}/consumables"),
        ("POST", "/api/consumables"),
    ],
    "products.edit": [
        ("PATCH", "/api/products/{product_id}"),
        ("DELETE", "/api/products/{product_id}/components/{component_id}"),
        ("DELETE", "/api/products/{product_id}/consumables/{consumable_link_id}"),
        ("PATCH", "/api/consumables/{consumable_id}"),
        ("POST", "/api/consumables/{consumable_id}/adjust"),
    ],
    "products.delete": [
        ("DELETE", "/api/products/{product_id}"),
        ("DELETE", "/api/consumables/{consumable_id}"),
    ],
    "alerts.read": [
        ("PATCH", "/api/alerts/{alert_id}/read"),
        ("POST", "/api/alerts/mark-all-read"),
        ("PATCH", "/api/alerts/{alert_id}/dismiss"),
    ],
}

# PATCH /api/jobs/{job_id}: callers without jobs.edit but with jobs.resubmit may
# only correct their own rejected submission (the handler enforces the limit).
ACTION_FALLBACKS: dict[tuple[str, str], str] = {
    ("PATCH", "/api/jobs/{job_id}"): "jobs.resubmit",
}

# Server read endpoints that exist only to feed one page.
PAGE_ROUTES: dict[str, list[tuple[str, str]]] = {
    "timeline": [("GET", "/api/timeline")],
    "analytics": [
        ("GET", "/api/analytics"),
        ("GET", "/api/analytics/failures"),
        ("GET", "/api/analytics/time-accuracy"),
    ],
    "education_reports": [("GET", "/api/education/usage-report")],
}

# Page keys whose data comes only from shared endpoints (used by other pages,
# the header, the sidebar or the native apps). Those endpoints stay governed by
# their action/role checks; the page key is enforced by the frontend RoleGate.
PAGE_SHARED: dict[str, str] = {
    "dashboard": "GET /api/stats, /api/jobs and /api/printers also feed the sidebar, Timeline and native apps",
    "jobs": "GET /api/jobs also feeds the dashboard, Timeline and native apps",
    "printers": "GET /api/printers also feeds every page that picks a printer",
    "models": "GET /api/models also feeds Upload, scheduling and orders",
    "spools": "GET /api/spools also feeds printer slot assignment",
    "cameras": "camera endpoints also feed printer cards and the TV dashboard",
    "calculator": "GET /api/pricing-config and model cost also feed models and orders",
    "upload": "the page only writes (upload.upload / upload.schedule / upload.delete)",
    "maintenance": "GET /api/maintenance/status also feeds the dashboard widget",
    "settings": "Settings reads are admin/superadmin-only per section",
    "admin": "user administration reads are admin-only",
    "audit": "GET /api/audit-logs is platform-superadmin-only",
    "branding": "GET /api/branding is public (login page and invoices)",
    "education": "Education reads are governed by Education workflow principals",
    "orders": "GET /api/orders also feeds global search and products",
    "products": "GET /api/products also feeds orders and global search",
    "alerts": "GET /api/alerts also feeds the header alert bell on every page",
}

_SUPERADMIN = "platform superadmin only (stricter than any matrix key)"
_SELF = "self-service on the caller's own account, credentials or devices"
_PUBLIC = "public or setup-flow route (no matrix role applies)"
_EDU = "Education workflow: governed by cost-center grants (Education principals)"
_ADMIN = "user and organization administration: admin role, no matrix key"

WRITE_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("POST", "/api/inventory/conflicts/resolve"): "admin role; audited explicit inventory conflict resolution is stricter than spools.edit",
    # Platform administration (superadmin)
    **{(m, p): _SUPERADMIN for m, p in [
        ("PUT", "/api/admin/oidc"), ("POST", "/api/admin/retention/cleanup"),
        ("POST", "/api/archives/reconcile-ownership"),
        ("POST", "/api/backups"), ("POST", "/api/backups/restore"), ("DELETE", "/api/backups/{filename}"),
        ("POST", "/api/backups/{filename}/stage"),
        ("PUT", "/api/branding"), ("POST", "/api/branding/favicon"), ("DELETE", "/api/branding/favicon"),
        ("POST", "/api/branding/logo"), ("DELETE", "/api/branding/logo"),
        ("PUT", "/api/config"), ("PUT", "/api/config/ip-allowlist"), ("PUT", "/api/config/mqtt-republish"),
        ("POST", "/api/config/mqtt-republish/test"), ("PUT", "/api/config/quiet-hours"), ("PUT", "/api/config/retention"),
        ("POST", "/api/groups"), ("DELETE", "/api/groups/{group_id}"),
        ("DELETE", "/api/license"), ("POST", "/api/license/activate"), ("POST", "/api/license/activation-request"),
        ("POST", "/api/license/reactivate"), ("POST", "/api/license/unactivate"),
        ("POST", "/api/license/unactivation-request"), ("POST", "/api/license/upload"),
        ("POST", "/api/license/trial-request"),  # device-signed Pro trial request (cert-trial2)
        ("POST", "/api/orgs"), ("DELETE", "/api/orgs/{org_id}"), ("POST", "/api/orgs/{org_id}/printers"),
        ("PUT", "/api/permissions"), ("POST", "/api/permissions/reset"),
        ("POST", "/api/push/vapid-key/rotate"),
        ("POST", "/api/report-schedules"), ("DELETE", "/api/report-schedules/{schedule_id}"),
        ("PATCH", "/api/report-schedules/{schedule_id}"), ("POST", "/api/report-schedules/{schedule_id}/run"),
        ("PUT", "/api/settings/education-mode"), ("PUT", "/api/settings/language"),
        ("POST", "/api/setup/complete"), ("POST", "/api/setup/printer"),
        ("PUT", "/api/smtp-config"),
        ("POST", "/api/system/troubleshooting/printer"), ("POST", "/api/system/troubleshooting/report"),
        ("POST", "/api/system/troubleshooting/server"),
        ("POST", "/api/system/notices/{kind}/dismiss"),  # license expiry / Pro trial notices (cert-trial)
        ("POST", "/api/vision/models"), ("DELETE", "/api/vision/models/{model_id}"),
        ("PATCH", "/api/vision/models/{model_id}/activate"), ("PATCH", "/api/vision/settings"),
        ("POST", "/api/webhooks"), ("PATCH", "/api/webhooks/{webhook_id}"), ("DELETE", "/api/webhooks/{webhook_id}"),
        ("POST", "/api/webhooks/{webhook_id}/test"),
    ]},
    # Own account, credentials and devices
    **{(m, p): _SELF for m, p in [
        ("PUT", "/api/alert-preferences"),
        ("POST", "/api/auth/biometric-refresh"), ("POST", "/api/auth/biometric-token"), ("DELETE", "/api/auth/biometric-token"),
        ("POST", "/api/auth/logout"), ("PUT", "/api/auth/me/theme"),
        ("DELETE", "/api/auth/mfa"), ("POST", "/api/auth/mfa/confirm"), ("POST", "/api/auth/mfa/enrollment/cancel"),
        ("POST", "/api/auth/mfa/enrollment/confirm"), ("POST", "/api/auth/mfa/enrollment/setup"),
        ("POST", "/api/auth/mfa/setup"), ("POST", "/api/auth/ws-token"),
        ("POST", "/api/push/live-activity"), ("PUT", "/api/push/preferences"), ("POST", "/api/push/register"),
        ("DELETE", "/api/push/register/{device_id}"), ("POST", "/api/push/subscribe"), ("DELETE", "/api/push/subscribe"),
        ("POST", "/api/push/test"),
        ("DELETE", "/api/sessions"), ("DELETE", "/api/sessions/{session_id}"),
        ("POST", "/api/tokens"), ("DELETE", "/api/tokens/{token_id}"),
        ("POST", "/api/cameras/{printer_id}/webrtc"),
    ]},
    # Public, login and setup flows
    **{(m, p): _PUBLIC for m, p in [
        ("POST", "/api/auth/forgot-password"), ("POST", "/api/auth/login"), ("POST", "/api/auth/mfa/verify"),
        ("POST", "/api/auth/oidc/exchange"), ("POST", "/api/auth/reset-password"),
        ("POST", "/api/setup/admin"), ("POST", "/api/setup/test-printer"),
    ]},
    # User, group and organization administration
    **{(m, p): _ADMIN for m, p in [
        ("PUT", "/api/admin/quotas/{user_id}"), ("DELETE", "/api/admin/sessions/{session_id}"),
        ("DELETE", "/api/admin/users/{user_id}/mfa"),
        ("PATCH", "/api/groups/{group_id}"),
        ("PATCH", "/api/orgs/{org_id}"), ("POST", "/api/orgs/{org_id}/members"), ("PUT", "/api/orgs/{org_id}/settings"),
        ("POST", "/api/users"), ("POST", "/api/users/import"), ("PATCH", "/api/users/{user_id}"),
        ("DELETE", "/api/users/{user_id}"), ("DELETE", "/api/users/{user_id}/erase"),
        ("POST", "/api/users/{user_id}/reset-password-email"),
    ]},
    # Admin-floor routes whose nearest key's default is looser: the stricter route check applies.
    ("POST", "/api/printers/bulk-update"): "admin role (stricter than printers.edit, which operators hold by default)",
    ("DELETE", "/api/archives/{archive_id}"): "admin role; archives have no matrix key",
    ("DELETE", "/api/projects/{project_id}"): "admin role; projects have no matrix key",
    ("DELETE", "/api/timelapses/{timelapse_id}"): "admin role; timelapses have no matrix key",
    ("POST", "/api/tags/rename"): "admin role; tags have no matrix key",
    ("DELETE", "/api/tags/{tag}"): "admin role; tags have no matrix key",
    ("POST", "/api/alerts/test-email"): "admin role and Pro email_notifications; no matrix key",
    ("DELETE", "/api/maintenance/tasks/{task_id}"): "admin role (stricter than maintenance.tasks, which operators hold by default)",
    ("POST", "/api/maintenance/seed-defaults"): "admin role (stricter than maintenance.tasks, which operators hold by default)",
    ("DELETE", "/api/maintenance/logs/{log_id}"): "admin role (stricter than maintenance.log, which operators hold by default)",
    ("POST", "/api/vision/training-data/{detection_id}/annotation-v2"): "admin role; Vigil AI training data has no matrix key",
    ("POST", "/api/vision/training-data/{detection_id}/label"): "admin role; Vigil AI training data has no matrix key",
    # Operator-floor routes with no matrix key
    ("DELETE", "/api/alerts/{alert_id}"): "operator role; deleting alerts has no matrix key (alerts.read covers read/dismiss)",
    ("PATCH", "/api/archives/{archive_id}"): "operator role; archives have no matrix key",
    ("PATCH", "/api/archives/{archive_id}/tags"): "operator role; archives have no matrix key",
    ("POST", "/api/profiles"): "operator role; slicer/printer profiles have no matrix key",
    ("POST", "/api/profiles/import"): "operator role; slicer/printer profiles have no matrix key",
    ("PUT", "/api/profiles/{profile_id}"): "operator role; slicer/printer profiles have no matrix key",
    ("DELETE", "/api/profiles/{profile_id}"): "operator role; slicer/printer profiles have no matrix key",
    ("POST", "/api/profiles/{profile_id}/revert/{revision_number}"): "operator role; slicer/printer profiles have no matrix key",
    ("POST", "/api/profiles/{profile_id}/revisions"): "operator role; slicer/printer profiles have no matrix key",
    ("POST", "/api/projects"): "operator role; projects have no matrix key",
    ("POST", "/api/projects/import"): "operator role; projects have no matrix key",
    ("PUT", "/api/projects/{project_id}"): "operator role; projects have no matrix key",
    ("POST", "/api/projects/{project_id}/archives"): "operator role; projects have no matrix key",
    ("POST", "/api/timelapses/{timelapse_id}/speed"): "operator role; timelapses have no matrix key",
    ("POST", "/api/timelapses/{timelapse_id}/trim"): "operator role; timelapses have no matrix key",
    ("PATCH", "/api/vision/detections/{detection_id}"): "operator role; Vigil AI detection review has no matrix key",
    ("POST", "/api/vision/detections/{detection_id}/dismiss-and-resume"): "operator role; Vigil AI detection review has no matrix key",
}

# Every /api/education/* write is an Education workflow route.
WRITE_EXEMPT_PREFIXES: dict[str, str] = {"/api/education/": _EDU}


def write_exemption(method: str, path: str):
    """Documented reason a write route has no matrix key, or None."""
    if (method, path) in WRITE_EXEMPTIONS:
        return WRITE_EXEMPTIONS[(method, path)]
    for prefix, reason in WRITE_EXEMPT_PREFIXES.items():
        # A workflow can acquire an explicit matrix key without inheriting
        # the broad historical Education exemption. Explicit exemptions above
        # remain visible to the shadowing contract.
        if path.startswith(prefix) and not any((method, path) in routes for routes in ACTION_ROUTES.values()):
            return reason
    return None
