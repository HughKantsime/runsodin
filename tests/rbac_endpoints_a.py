"""Route expectations for the RBAC census and live matrix."""
from rbac_expectations import _ONLINE_LICENSE, _pub, _setup_locked, _api_read, _op_write, _admin_only, _admin_read

ENDPOINTS = [
    # =========================================================================
    # 1. Auth & Setup
    # =========================================================================
    ("POST", "/api/auth/login", {"no_headers": 200, "api_key_only": None, "viewer": None, "operator": None, "admin": None},
     {"username": "x", "password": "x"}, "Login (will 401/422 but won't crash)"),
    ("GET",  "/api/auth/me/theme", _api_read(), None, "Get user theme"),
    ("PUT",  "/api/auth/me/theme", _api_read(), {"accent_color": "#ff0000"}, "Set user theme"),
    ("GET",  "/api/auth/me", {"no_headers": 401, "api_key_only": 200, "viewer": 200, "operator": 200, "admin": 200},
     None, "Current user — api_key_only returns null user or 200"),
    ("GET",  "/api/auth/oidc/config", _pub(), None, "OIDC config (public for login page)"),
    ("GET",  "/api/setup/status", _pub(), None, "Setup status"),
    ("POST", "/api/setup/admin", _setup_locked(),
     {"username": "t", "email": "t@t.com", "password": "TestPass1!"}, "Setup locked"),
    ("POST", "/api/setup/test-printer", _setup_locked(),
     {"api_type": "bambu", "api_host": "192.168.1.1"}, "Setup locked"),
    ("POST", "/api/setup/printer", _setup_locked(), {"name": "Test"}, "Setup locked"),
    ("POST", "/api/setup/complete", _setup_locked(), None, "Setup locked"),
    ("GET",  "/health", _pub(), None, "Health check"),

    # =========================================================================
    # 2. Printers
    # =========================================================================
    ("GET",  "/api/printers", _api_read(), None, "List printers"),
    # POST /api/printers — from grep: line 458 delete_printer has require_role("operator") 
    # but create returned 403 for operator AND admin in test. Needs investigation.
    # For now mark as admin_only (safest assumption — may be body/license issue)
    ("POST", "/api/printers", _admin_only(),
     {"name": "RBAC Temp Printer", "api_type": "bambu"}, "Create printer — printers.add (admin by default)"),
    ("POST", "/api/printers/reorder", _op_write(), {"order": {}}, "Reorder printers"),
    ("POST", "/api/printers/test-connection", _op_write(),
     {"api_type": "bambu", "api_host": "192.168.99.99"}, "Test connection"),
    ("GET", "/api/printers/{printer_id}/next-suggestion", _api_read(), None, "Recomputed next job"),
    ("POST", "/api/printers/{printer_id}/start-next", _op_write(), {"suggested_job_id": 1, "suggestion_fingerprint": "0" * 64, "request_id": "00000000-0000-4000-8000-000000000001", "bed_clear_confirmed": True}, "Owner-confirmed start next"),
    ("GET",  "/api/printers/{printer_id}", _api_read(), None, "Get printer"),
    ("PATCH", "/api/printers/{printer_id}", _op_write(), {"nickname": "rbac-test"}, "Update printer"),
    # DELETE printers: from grep line 458 → require_role("operator"). Operator CAN delete.
    ("DELETE", "/api/printers/{printer_id}", _admin_only(), None, "Delete printer — printers.delete (admin by default)"),
    ("POST", "/api/printers/{printer_id}/stop", _op_write(), None, "Emergency stop"),
    ("POST", "/api/printers/{printer_id}/pause", _op_write(), None, "Pause"),
    ("POST", "/api/printers/{printer_id}/resume", _op_write(), None, "Resume"),
    ("POST", "/api/printers/{printer_id}/lights", _op_write(), None, "Lights"),
    ("POST", "/api/printers/{printer_id}/sync-ams", _op_write(), None, "Sync AMS"),
    ("POST", "/api/printers/{printer_id}/clear-errors", _op_write(), None, "Clear HMS errors (Bambu)"),
    ("POST", "/api/printers/{printer_id}/skip-objects", _op_write(), {"object_ids": [0]}, "Skip objects (Bambu)"),
    ("POST", "/api/printers/{printer_id}/speed", _op_write(), {"speed": 2}, "Set print speed (Bambu)"),
    ("GET",  "/api/printers/{printer_id}/live-status", _api_read(), None, "Live status"),
    ("GET",  "/api/printers/live-status", _api_read(), None, "All live status"),

    # =========================================================================
    # 3. Printer Filament Slots
    # =========================================================================
    ("GET",  "/api/printers/{printer_id}/slots", _api_read(), None, "List slots"),
    ("PATCH", "/api/printers/{printer_id}/slots/1", _op_write(), {"filament_type": "PLA"}, "Update slot"),
    ("PATCH", "/api/printers/{printer_id}/slots/1/manual-assign", _op_write(),
     {"filament_type": "PLA", "color": "Red"}, "Manual assign"),
    ("POST", "/api/printers/{printer_id}/slots/1/assign", _op_write(), None, "Assign spool"),
    ("POST", "/api/printers/{printer_id}/slots/1/confirm", _op_write(), None, "Confirm assign"),
    ("GET",  "/api/printers/{printer_id}/slots/needs-attention", _api_read(), None, "Needs attention"),
    ("GET",  "/api/printers/{printer_id}/unmatched-slots", _api_read(), None, "Unmatched slots"),

    # =========================================================================
    # 4. Smart Plug — config is operator (per grep lines 6208, 6243)
    # =========================================================================
    ("GET",    "/api/printers/{printer_id}/plug", _api_read(), None, "Get plug config"),
    ("PUT",    "/api/printers/{printer_id}/plug", _op_write(), {}, "Update plug config — operator per code"),
    ("DELETE", "/api/printers/{printer_id}/plug", _op_write(), None, "Remove plug config — operator per code"),
    ("POST",   "/api/printers/{printer_id}/plug/on", _op_write(), None, "Plug on"),
    ("POST",   "/api/printers/{printer_id}/plug/off", _op_write(), None, "Plug off"),
    ("POST",   "/api/printers/{printer_id}/plug/toggle", _op_write(), None, "Plug toggle"),
    ("GET",    "/api/printers/{printer_id}/plug/energy", _api_read(), None, "Plug energy"),
    ("GET",    "/api/printers/{printer_id}/plug/state", _api_read(), None, "Plug state"),

    # =========================================================================
    # 5. AMS Environment & Fan Controls
    # =========================================================================
    ("GET", "/api/printers/{printer_id}/ams/environment", _api_read(), None, "AMS environment"),
    ("GET", "/api/printers/{printer_id}/ams/current", _api_read(), None, "AMS current"),
    ("POST", "/api/printers/{printer_id}/fan", _op_write(), {"fan": "auxiliary", "speed": 128}, "Set fan speed (Bambu)"),
    ("POST", "/api/printers/{printer_id}/ams/refresh", _op_write(), None, "AMS RFID re-read (Bambu)"),
    ("PUT",  "/api/printers/{printer_id}/ams/{ams_id}/slots/{slot_id}", _op_write(),
     {"material": "PLA", "color": "#FF0000"}, "Configure AMS slot (Bambu)"),
    ("POST", "/api/printers/{printer_id}/plate-cleared", _op_write(), None, "Plate cleared confirmation"),

    # =========================================================================
    # 6. Bambu-Specific
    # =========================================================================
    ("POST", "/api/bambu/test-connection", _op_write(),
     {"ip_address": "192.168.99.99", "serial_number": "TEST", "access_code": "TEST"}, "Bambu test connection"),
    ("GET",  "/api/bambu/filament-types", _pub(), None, "Filament types (public)"),
    ("POST", "/api/printers/{printer_id}/bambu/sync-ams", _op_write(), None, "Bambu sync AMS"),

    # =========================================================================
    # 7. Jobs
    # =========================================================================
    ("GET",  "/api/jobs", _api_read(), None, "List jobs"),
    # Viewer CAN create jobs (approval workflow) — api_key_only can too apparently
    ("POST", "/api/jobs",
     {"no_headers": 401, "api_key_only": 200, "viewer": 200, "operator": 200, "admin": 200},
     {"item_name": "RBAC Test Job"}, "Create job — all authed users"),
    ("POST", "/api/jobs/bulk", _op_write(), [{"item_name": "RBAC Bulk 1"}], "Bulk create"),
    ("PATCH", "/api/jobs/reorder", _op_write(), {"job_ids": []}, "Reorder jobs"),
    ("GET",  "/api/jobs/{job_id}", _api_read(), None, "Get job"),
    ("PATCH", "/api/jobs/{job_id}", _op_write(), {"notes": "rbac test"}, "Update job"),
    ("DELETE", "/api/jobs/{job_id}", _op_write(), None, "Delete job — SKIP actual"),
    ("POST", "/api/jobs/{job_id}/start", _op_write(), None, "Start job"),
    ("POST", "/api/jobs/{job_id}/complete", _op_write(), None, "Complete job"),
    ("POST", "/api/jobs/{job_id}/fail", _op_write(), None, "Fail job"),
    ("POST", "/api/jobs/{job_id}/cancel", _op_write(), None, "Cancel job"),
    ("POST", "/api/jobs/{job_id}/reset", _op_write(), None, "Reset job"),
    # approve/reject/resubmit: STATEFUL — 403 means "wrong state" not "wrong role"
    # Skip these from pure RBAC test; test in Phase 3 with proper state fixtures
    ("POST", "/api/jobs/batch", _op_write(), {"item_name": "batch_test", "printer_ids": [1]}, "Batch send job"),
    ("POST", "/api/jobs/{job_id}/repeat", _op_write(), None, "Repeat job"),
    ("POST", "/api/jobs/{job_id}/link-print", _op_write(), None, "Link to print"),
    ("PATCH", "/api/jobs/{job_id}/move", _op_write(),
     {"printer_id": 1, "scheduled_start": "2026-03-01T00:00:00"}, "Move job"),
    ("PATCH", "/api/jobs/{job_id}/failure", _op_write(), {}, "Update failure reason"),
    ("GET",  "/api/jobs/filament-check", _api_read(), None, "Filament compatibility check"),
    ("GET",  "/api/failure-reasons", _pub(), None, "Failure reasons (public)"),

    # =========================================================================
    # 8. Print Files
    # =========================================================================
    ("GET",  "/api/print-files", _api_read(), None, "List print files"),
    ("GET",  "/api/print-files/1", _api_read(), None, "Get print file"),
    ("DELETE", "/api/print-files/1", _op_write(), None, "Delete print file"),

    # =========================================================================
    # 9. Print Jobs (MQTT Tracking)
    # =========================================================================
    ("GET", "/api/print-jobs", _api_read(), None, "MQTT print history"),
    ("GET", "/api/print-jobs/stats", _api_read(), None, "Print job stats"),
    ("GET", "/api/print-jobs/unlinked", _api_read(), None, "Unlinked prints"),

    # =========================================================================
    # 10. Models — POST requires operator (grep line 1472)
    # =========================================================================
    ("GET",  "/api/models", _api_read(), None, "List models"),
    ("POST", "/api/models", _op_write(), {"name": "RBAC Test Model"}, "Create model — operator+"),
    ("GET",  "/api/models-with-pricing", _api_read(), None, "Models with pricing"),
    ("GET",  "/api/models/{model_id}", _api_read(), None, "Get model"),
    ("PATCH", "/api/models/{model_id}", _op_write(), {"notes": "rbac"}, "Update model"),
    ("DELETE", "/api/models/{model_id}", _admin_only(), None, "Delete model — models.delete (admin by default) — SKIP actual"),
    ("GET",  "/api/models/{model_id}/cost", _api_read(), None, "Model cost"),
    ("GET",  "/api/models/{model_id}/mesh", _api_read(), None, "Model mesh"),
    ("GET",  "/api/models/{model_id}/variants", _api_read(), None, "Model variants"),

    # =========================================================================
    # 11. Spools
    # =========================================================================
    ("GET",  "/api/spools/export", _api_read(), None, "Export spools CSV"),
    ("GET",  "/api/spools/low-stock", _api_read(), None, "Low stock spools"),
    ("GET",  "/api/spools", _api_read(), None, "List spools"),
    ("POST", "/api/spools", _op_write(), {"filament_id": 1}, "Create spool"),
    ("GET",  "/api/spools/{spool_id}", _api_read(), None, "Get spool"),
    ("PATCH", "/api/spools/{spool_id}", _op_write(), {"notes": "rbac"}, "Update spool"),
    ("DELETE", "/api/spools/{spool_id}", _op_write(), None, "Delete spool — spools.delete (operator/admin) — SKIP actual"),
    ("POST", "/api/spools/{spool_id}/load", _op_write(),
     {"printer_id": 1, "slot_number": 1}, "Load spool"),
    ("POST", "/api/spools/{spool_id}/unload", _op_write(), None, "Unload spool"),
    ("POST", "/api/spools/{spool_id}/use", _op_write(), {"weight_used_g": 0.1}, "Use spool"),
    ("POST", "/api/spools/{spool_id}/weigh", _op_write(), {"gross_weight_g": 500.0}, "Weigh spool"),
    ("GET",  "/api/spools/{spool_id}/qr", _api_read(), None, "Spool QR"),
    ("GET",  "/api/spools/{spool_id}/label", _pub(), None, "Spool label (public)"),
    ("GET",  "/api/spools/labels/batch?spool_ids=1", _pub(), None, "Batch labels (public)"),
    ("GET",  "/api/spools/lookup/TEST_QR", _api_read(), None, "QR lookup"),
    ("POST", "/api/spools/scan-assign", _op_write(),
     {"qr_code": "test", "printer_id": 1, "slot": 1}, "Scan assign"),

    # =========================================================================
    # 12. Filaments Library
    # =========================================================================
    ("GET",  "/api/filaments", _api_read(), None, "List filaments"),
    ("POST", "/api/filaments", _op_write(),
     {"brand": "RBAC", "name": "RBAC Filament"}, "Add filament"),
    ("GET",  "/api/filaments/combined", _api_read(), None, "Combined filaments"),
    ("GET",  "/api/filaments/{filament_id}", _api_read(), None, "Get filament"),
    ("PATCH", "/api/filaments/{filament_id}", _op_write(), {"name": "RBAC Updated"}, "Update filament"),
    ("DELETE", "/api/filaments/{filament_id}", _op_write(), None, "Delete filament — SKIP actual"),

    # =========================================================================
    # 13. Products
    # =========================================================================
    ("GET",  "/api/products", _api_read(), None, "List products"),
    ("POST", "/api/products/production/outside-prints/999999/bind",
     {"no_headers":401,"api_key_only":403,"viewer":403,"operator":422,"admin":422},
     {}, "Outside print binding requires production authority and explicit attestation"),
    ("POST", "/api/products", _op_write(), {"name": "RBAC Temp Product"}, "Create product"),
    ("GET",  "/api/products/{product_id}", _api_read(), None, "Get product"),
    ("PATCH", "/api/products/{product_id}", _op_write(), {"description": "rbac"}, "Update product"),
    # DELETE products: grep line 7227 → require_role("operator"). Operator CAN delete.
    # products.delete defaults to admin (the matrix default on main too, now server-enforced:
    # FEATURES.md §11.3 (Permission matrix); core/rbac_policy.py).
    ("DELETE", "/api/products/{product_id}", _admin_only(), None, "Delete product — products.delete (admin by default)"),
    ("POST", "/api/products/{product_id}/components", _op_write(), {"model_id": 1}, "Add BOM component"),

    # =========================================================================
    # 13b. Projects
    # =========================================================================
    ("GET",    "/api/projects",                          _api_read(), None,                       "List projects"),
    ("POST",   "/api/projects",                          _op_write(), {"name": "RBAC Test Proj"}, "Create project"),
    ("GET",    "/api/projects/{project_id}",             _api_read(), None,                       "Get project"),
    ("PUT",    "/api/projects/{project_id}",             _op_write(), {"name": "Updated"},        "Update project"),
    ("DELETE", "/api/projects/{project_id}",             _admin_only(), None,                     "Delete project (soft)"),
    ("POST",   "/api/projects/{project_id}/archives",    _op_write(), {"archive_ids": []},        "Bulk assign archives"),
    ("GET",    "/api/projects/{project_id}/export",      _op_write(), None,                       "Export project ZIP"),
    ("POST",   "/api/projects/import",                   _op_write(), None,                       "Import project ZIP"),

    # =========================================================================
    # 14. Orders — DELETE: grep line 7381 → require_role("operator")
    # =========================================================================
    ("GET",  "/api/orders", _api_read(), None, "List orders"),
    ("POST", "/api/orders", _op_write(), {}, "Create order"),
    ("GET",  "/api/orders/{order_id}", _api_read(), None, "Get order"),
    ("PATCH", "/api/orders/{order_id}", _admin_only(), {"notes": "rbac"}, "Update order — admin per code"),
    ("DELETE", "/api/orders/{order_id}", _op_write(), None, "Delete order — operator per code"),
    ("POST", "/api/orders/{order_id}/items", _op_write(), {"product_id": 1}, "Add item"),
    ("POST", "/api/orders/{order_id}/schedule", _op_write(), None, "Schedule order"),
    ("PATCH", "/api/orders/{order_id}/ship", _op_write(), {}, "Ship order"),

    # =========================================================================
    # 15. Scheduler
    # =========================================================================
    ("POST", "/api/scheduler/run", _op_write(), None, "Run scheduler"),
    ("GET",  "/api/scheduler/runs", _api_read(), None, "Scheduler history"),

    # =========================================================================
    # 16. Timeline
    # =========================================================================
    ("GET", "/api/timeline", _api_read(), None, "Timeline"),

    # =========================================================================
    # 17. Cameras
    # =========================================================================
    ("GET",  "/api/cameras", _api_read(), None, "List cameras"),
    ("PATCH", "/api/cameras/{printer_id}/toggle", _op_write(), None, "Toggle camera"),
    ("GET",  "/api/cameras/{printer_id}/stream", _api_read(), None, "Camera stream"),

    # =========================================================================
    # 18. Maintenance
    # =========================================================================
    ("GET",  "/api/maintenance/tasks", _api_read(), None, "List tasks"),
    ("POST", "/api/maintenance/tasks", _op_write(), {"name": "RBAC Temp"}, "Create task"),
    ("PATCH", "/api/maintenance/tasks/{maintenance_task_id}", _op_write(),
     {"description": "rbac"}, "Update task"),
    ("DELETE", "/api/maintenance/tasks/{maintenance_task_id}", _admin_only(), None, "Delete task"),
    ("GET",  "/api/maintenance/logs", _api_read(), None, "List logs"),
    ("POST", "/api/maintenance/logs", _op_write(),
     {"printer_id": 1, "task_name": "RBAC Temp"}, "Create log"),
    ("DELETE", "/api/maintenance/logs/{maintenance_log_id}", _admin_only(), None, "Delete log"),
    ("GET",  "/api/maintenance/status", _api_read(), None, "Maintenance status"),
    ("POST", "/api/maintenance/seed-defaults", _admin_only(), None, "Seed defaults"),

    # =========================================================================
    # 19. Analytics & Export (AFTER PATCH: exports require operator+)
    # =========================================================================
    ("GET", "/api/analytics", _api_read(), None, "Analytics"),
    ("GET", "/api/stats", _api_read(), None, "Dashboard stats"),
    ("GET", "/api/export/jobs", _op_write(), None, "Export jobs CSV — operator+ after patch"),
    ("GET", "/api/export/spools", _op_write(), None, "Export spools CSV"),
    ("GET", "/api/export/models", _op_write(), None, "Export models CSV"),
    ("GET", "/api/export/filament-usage", _op_write(), None, "Export filament usage CSV"),

    # =========================================================================
    # 20. Pricing
    # =========================================================================
    ("GET", "/api/pricing-config", _api_read(), None, "Get pricing config"),
    ("PUT", "/api/pricing-config", _admin_only(), {}, "Update pricing config"),

    # =========================================================================
    # 21. Alerts
    # =========================================================================
    ("GET",  "/api/alerts", _api_read(), None, "List alerts"),
    ("GET",  "/api/alerts/unread-count", _api_read(), None, "Unread count"),
    ("GET",  "/api/alerts/summary", _api_read(), None, "Alert summary"),
    ("PATCH", "/api/alerts/1/read", _api_read(), None, "Mark read"),
    ("POST", "/api/alerts/mark-all-read", _api_read(), None, "Mark all read"),
    ("PATCH", "/api/alerts/1/dismiss", _api_read(), None, "Dismiss"),
    ("GET",  "/api/alert-preferences", _api_read(), None, "Alert prefs"),
    ("PUT",  "/api/alert-preferences", _api_read(), {"preferences": []}, "Update prefs (own)"),
    ("GET",  "/api/smtp-config", _admin_only(), None, "SMTP config"),
    ("PUT",  "/api/smtp-config", _admin_only(),
     {"enabled": False, "host": "", "port": 587, "username": "", "password": "", "from_address": "", "use_tls": True},
     "Update SMTP"),
    ("POST", "/api/alerts/test-email", _admin_only(), None, "Test email"),
    ("GET",  "/api/admin/logs", _admin_only(), None, "Get log lines"),
    ("GET",  "/api/admin/logs/stream", _admin_only(), None, "Stream logs (SSE)"),
    ("GET",  "/api/admin/support-bundle", _admin_only(), None, "Download support bundle"),
    ("POST", "/api/users/{user_id}/reset-password-email", _admin_only(), None, "Reset password & email"),
    ("POST", "/api/auth/forgot-password", _pub(), {"email": "test@test.com"}, "Forgot password"),
    ("POST", "/api/auth/reset-password", _pub(), {"token": "invalid", "new_password": "Test1234!"}, "Reset password"),
    ("GET",  "/api/auth/capabilities", _pub(), None, "Auth capabilities"),
    ("GET",  "/api/archives", _api_read(), None, "List print archives"),
    ("GET",  "/api/archives/compare", _api_read(), None, "Compare two archives"),
    ("GET",  "/api/archives/log", _api_read(), None, "Print log"),
    ("GET",  "/api/archives/log/export", _api_read(), None, "Export print log CSV"),
    ("GET",  "/api/archives/{archive_id}", _api_read(), None, "Get print archive"),
    ("PATCH", "/api/archives/{archive_id}", _op_write(), {"notes": "test"}, "Update archive notes"),
    ("PATCH", "/api/archives/{archive_id}/tags", _op_write(), {"tags": ["test"]}, "Update archive tags"),
    ("GET",  "/api/archives/{archive_id}/ams-preview", _api_read(), None, "AMS preview for archive"),
    ("POST", "/api/archives/{archive_id}/reprint", _op_write(), {"printer_id": 1}, "Reprint archive"),
    ("DELETE", "/api/archives/{archive_id}", _admin_only(), None, "Delete archive"),
    ("GET",  "/api/tags", _api_read(), None, "List all tags"),
    ("POST", "/api/tags/rename", _admin_only(), {"old": "test", "new": "test2"}, "Rename tag"),
    ("DELETE", "/api/tags/{tag}", _admin_only(), None, "Delete tag"),
    ("GET",  "/api/files/{file_id}/preview-model", _api_read(), None, "3D model preview"),
    ("GET",  "/api/overlay/{printer_id}", _pub(), None, "OBS streaming overlay (public, no auth)"),

    # =========================================================================
    # 22. Push
    # =========================================================================
    ("GET",  "/api/push/vapid-key", _pub(), None, "VAPID key (public)"),
    ("POST", "/api/push/subscribe", _api_read(),
     {"endpoint": "https://example.com/push", "p256dh_key": "test", "auth_key": "test"}, "Subscribe push"),
    ("DELETE", "/api/push/subscribe", _api_read(), None, "Unsubscribe push"),

    # =========================================================================
    # 23. Webhooks
    # =========================================================================
    ("GET",    "/api/webhooks", _admin_only(), None, "List webhooks"),
    ("POST",   "/api/webhooks", _admin_only(), {}, "Create webhook"),
    ("PATCH",  "/api/webhooks/{webhook_id}", _admin_only(), {}, "Update webhook"),
    ("DELETE", "/api/webhooks/{webhook_id}", _admin_only(), None, "Delete webhook — SKIP"),
    ("POST",   "/api/webhooks/{webhook_id}/test", _admin_only(), None, "Test webhook"),

    # =========================================================================
    # 24. Users
    # =========================================================================
    ("GET",    "/api/users", _admin_only(), None, "List users"),
    ("POST",   "/api/users", _admin_only(),
     {"username": "rbac_tmp_99", "email": "rbac99@test.local", "password": "TmpPass1!"}, "Create user"),
    ("PATCH",  "/api/users/1", _admin_only(), {"email": "test@test.local"}, "Update user"),
    ("DELETE", "/api/users/99999", _admin_only(), None, "Delete user (nonexistent)"),

    # =========================================================================
    # 25-26. OIDC & Permissions
    # =========================================================================
    ("GET", "/api/admin/oidc", _admin_only(), None, "OIDC config"),
    ("PUT", "/api/admin/oidc", _admin_only(), {}, "Update OIDC"),
]
