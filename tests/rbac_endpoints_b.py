"""Route expectations for the RBAC census and live matrix."""
from rbac_expectations import _ONLINE_LICENSE, _pub, _setup_locked, _api_read, _op_write, _admin_only, _admin_read

ENDPOINTS = [
    ("GET",  "/api/permissions", _pub(), None, "Permissions (public)"),
    ("PUT",  "/api/permissions", _admin_only(), {"page_access": {}, "action_access": {}}, "Update permissions"),
    ("POST", "/api/permissions/reset", _admin_only(), None, "Reset permissions"),

    # =========================================================================
    # 27. Branding
    # =========================================================================
    ("GET",    "/api/branding", _pub(), None, "Branding (public)"),
    ("PUT",    "/api/branding", _admin_only(), {"app_name": "O.D.I.N."}, "Update branding"),
    ("DELETE", "/api/branding/logo", _admin_only(), None, "Remove logo"),

    # =========================================================================
    # 28. License
    # =========================================================================
    ("GET",    "/api/license", _pub(), None, "License (public)"),
    ("DELETE", "/api/license", _admin_only(), None, "Remove license — SKIP"),

    # =========================================================================
    # 29. System Config (AFTER PATCH: GET mqtt-republish & quiet-hours → admin)
    # =========================================================================
    ("GET", "/api/config", _api_read(), None, "Get config"),
    ("PUT", "/api/config", _admin_only(), {}, "Update config"),
    ("GET", "/api/config/require-job-approval", _pub(), None, "Job approval (public)"),
    ("PUT", "/api/config/require-job-approval", _admin_only(),
     {"enabled": False}, "Set job approval — INVESTIGATE body schema"),
    ("GET", "/api/config/mqtt-republish", _admin_only(), None, "MQTT republish config — admin after patch"),
    ("PUT", "/api/config/mqtt-republish", _admin_only(), {}, "Update MQTT republish"),
    ("POST", "/api/push/test", _admin_only(), None, "Test native push — caller devices only"),
    ("POST", "/api/config/mqtt-republish/test", _admin_only(), None, "Test MQTT republish — admin after patch"),
    ("GET", "/api/config/quiet-hours", _admin_only(), None, "Quiet hours — admin after patch"),
    ("PUT", "/api/config/quiet-hours", _admin_only(), {}, "Update quiet hours"),

    # =========================================================================
    # 30. Backups (AFTER PATCH: list requires admin)
    # =========================================================================
    ("GET",    "/api/backups", _admin_only(), None, "List backups — admin after patch"),
    ("POST",   "/api/backups", _admin_only(), None, "Create backup"),
    ("GET",    "/api/backups/{backup_filename}", _admin_only(), None, "Download backup"),
    ("DELETE", "/api/backups/{backup_filename}", _admin_only(), None, "Delete backup — SKIP"),

    # =========================================================================
    # 31. Audit Logs
    # =========================================================================
    ("GET", "/api/audit-logs", _admin_only(), None, "Audit logs"),

    # =========================================================================
    # 32. Settings
    # =========================================================================
    ("GET", "/api/settings/language", _api_read(), None, "Get language"),
    ("PUT", "/api/settings/language", _admin_only(), {}, "Set language"),
    ("GET", "/api/settings/energy-rate", _api_read(), None, "Get energy rate"),
    ("PUT", "/api/settings/energy-rate", _admin_only(), {}, "Set energy rate"),

    # =========================================================================
    # 33. Search & Misc
    # =========================================================================
    ("GET", "/api/search?q=test", _api_read(), None, "Global search"),
    ("GET", "/api/hms-codes/0500040000020002", _pub(), None, "HMS code"),
    ("GET", "/metrics", _pub(), None, "Prometheus metrics"),

    # =========================================================================
    # 34. Spoolman Integration
    # =========================================================================
    ("POST", "/api/spoolman/sync", _op_write(), None, "Spoolman sync"),
    ("GET",  "/api/spoolman/spools", _pub(), None, "Spoolman spools (public)"),
    ("GET",  "/api/spoolman/filaments", _pub(), None, "Spoolman filaments (public)"),
    ("GET",  "/api/spoolman/test", _pub(), None, "Spoolman test (public)"),

    # =========================================================================
    # 35. Admin Endpoints
    # =========================================================================
    ("GET",    "/api/admin/sessions",                  _admin_read(), None,           "List all sessions — admin"),
    ("DELETE", "/api/admin/sessions/{session_id}",     _admin_only(), None,           "Revoke session — admin"),
    ("DELETE", "/api/admin/users/{user_id}/mfa",       _admin_only(), None,           "Reset user MFA — admin"),
    ("GET",    "/api/admin/quotas",                    _admin_read(), None,           "List quotas — admin"),
    ("PUT",    "/api/admin/quotas/{user_id}",          _admin_only(), {"limit": 100}, "Set user quota"),
    ("POST",   "/api/admin/retention/cleanup",         _admin_only(), None,           "Run retention cleanup"),

    # =========================================================================
    # 36. Auth — MFA / OIDC / Sessions / Tokens / Quotas
    # =========================================================================
    # MFA (require_role("viewer") — all authed users including api_key_only)
    ("GET",    "/api/auth/mfa/status",   _api_read(), None,                "MFA status (own)"),
    ("POST",   "/api/auth/mfa/setup",    _api_read(), None,                "Start MFA setup"),
    ("POST",   "/api/auth/mfa/confirm",  _api_read(), {"token": "000000"}, "Confirm MFA setup"),
    ("POST",   "/api/auth/mfa/verify",   _pub(),      {"token": "000000"}, "Verify MFA (public second-factor)"),
    ("DELETE", "/api/auth/mfa",          _api_read(), None,                "Disable own MFA"),
    # OIDC (no auth dependency — public)
    ("GET",    "/api/auth/oidc/login",    _pub(), None,             "OIDC login redirect"),
    ("GET",    "/api/auth/oidc/callback", _pub(), None,             "OIDC callback"),
    ("POST",   "/api/auth/oidc/exchange", _pub(), {"code": "test"}, "OIDC token exchange"),
    # Logout — intentionally public (no-op when unauthed). Skip JWT role tests:
    # calling logout with a live test token blacklists it, breaking subsequent tests.
    ("POST",   "/api/auth/logout",  _pub(), None, "Logout (public no-op when unauthed)"),
    # WS token (get_current_user)
    ("POST",   "/api/auth/ws-token",  _api_read(), None, "Get WebSocket token"),
    # Sessions (require_role("viewer") — own sessions)
    ("GET",    "/api/sessions",               _api_read(), None, "List own sessions"),
    ("DELETE", "/api/sessions",               _api_read(), None, "Revoke all own sessions"),
    ("DELETE", "/api/sessions/{session_id}",  _api_read(), None, "Revoke own session"),
    # API tokens (require_role("viewer"))
    ("GET",    "/api/tokens",             _api_read(), None,                  "List API tokens"),
    ("POST",   "/api/tokens",             _api_read(), {"name": "RBAC Test"}, "Create API token"),
    ("DELETE", "/api/tokens/{token_id}",  _api_read(), None,                  "Revoke API token"),
    # Quotas — own usage
    ("GET",    "/api/quotas",  _api_read(), None, "Get own quota usage"),

    # =========================================================================
    # 37. Users — missing actions (template forms)
    # =========================================================================
    ("PATCH",  "/api/users/{user_id}",        _admin_only(), {"email": "rbac@test.local"}, "Update user (template)"),
    ("DELETE", "/api/users/{user_id}",        _admin_only(), None, "Delete user (template)"),
    ("GET",    "/api/users/{user_id}/export", _admin_only(), None, "Export user data (GDPR — admin or self; IDOR blocks non-admin)"),
    ("DELETE", "/api/users/{user_id}/erase",  _admin_only(), None, "Erase user data (GDPR)"),
    ("POST",   "/api/users/import",           _admin_only(), None, "Import users"),

    # =========================================================================
    # 38. Groups & Orgs
    # =========================================================================
    # Groups (GETs require operator, writes require admin)
    ("GET",    "/api/groups",              _op_write(),   None,                        "List groups (operator+)"),
    ("GET",    "/api/groups/{group_id}",   _admin_only(), None,                        "Get group (admin only — IDOR blocks non-members)"),
    ("POST",   "/api/groups",              _admin_only(), {"name": "RBAC Test Group"}, "Create group"),
    ("PATCH",  "/api/groups/{group_id}",   _admin_only(), {"name": "Updated"},         "Update group"),
    ("DELETE", "/api/groups/{group_id}",   _admin_only(), None,                        "Delete group"),
    # Orgs (all require admin)
    ("GET",    "/api/orgs",                       _admin_only(), None,                      "List orgs"),
    ("POST",   "/api/orgs",                       _admin_only(), {"name": "RBAC Test Org"}, "Create org"),
    ("PATCH",  "/api/orgs/{org_id}",              _admin_only(), {"name": "Updated"},        "Update org"),
    ("DELETE", "/api/orgs/{org_id}",              _admin_only(), None,                       "Delete org"),
    ("GET",    "/api/orgs/{org_id}/settings",     _admin_only(), None,                       "Get org settings"),
    ("PUT",    "/api/orgs/{org_id}/settings",     _admin_only(), {},                         "Update org settings"),
    ("POST",   "/api/orgs/{org_id}/members",      _admin_only(), {"user_id": 1},             "Add org member"),
    ("POST",   "/api/orgs/{org_id}/printers",     _admin_only(), {"printer_id": 1},          "Add org printer"),

    # =========================================================================
    # 39. Alerts — template forms (hardcoded /1 entries retained above)
    # =========================================================================
    ("PATCH", "/api/alerts/{alert_id}/read",    _api_read(), None, "Mark alert read (template)"),
    ("PATCH", "/api/alerts/{alert_id}/dismiss", _api_read(), None, "Dismiss alert (template)"),

    # =========================================================================
    # 40. Backups — restore action
    # =========================================================================
    ("POST", "/api/backups/restore", _admin_only(), None, "Restore backup from file"),

    # =========================================================================
    # 41. Branding — file uploads
    # =========================================================================
    ("POST", "/api/branding/logo",    _admin_only(), None, "Upload logo"),
    ("POST", "/api/branding/favicon", _admin_only(), None, "Upload favicon"),

    # =========================================================================
    # 42. Cameras — WebRTC
    # =========================================================================
    ("POST", "/api/cameras/{printer_id}/webrtc", _api_read(), None, "WebRTC offer"),

    # =========================================================================
    # 43. Config — additional endpoints
    # =========================================================================
    ("GET", "/api/config/ip-allowlist",  _admin_read(), None,              "Get IP allowlist"),
    ("PUT", "/api/config/ip-allowlist",  _admin_only(), {},                "Update IP allowlist"),
    ("GET", "/api/config/require-mfa",   _admin_read(), None,              "Get MFA enforcement config"),
    ("PUT", "/api/config/require-mfa",   _admin_only(), {"enabled": False}, "Set MFA enforcement"),
    ("GET", "/api/config/retention",     _admin_read(), None,              "Get data retention config"),
    ("PUT", "/api/config/retention",     _admin_only(), {},                "Update data retention config"),

    # =========================================================================
    # 44. Consumables
    # =========================================================================
    ("GET",    "/api/consumables",                        _api_read(),   None,                         "List consumables"),
    ("POST",   "/api/consumables",                        _op_write(),   {"name": "PLA", "unit": "g"}, "Create consumable"),
    ("GET",    "/api/consumables/low-stock",              _api_read(),   None,                         "Low-stock consumables"),
    ("GET",    "/api/consumables/{consumable_id}",        _api_read(),   None,                         "Get consumable"),
    ("PATCH",  "/api/consumables/{consumable_id}",        _op_write(),   {"name": "Updated"},          "Update consumable"),
    ("DELETE", "/api/consumables/{consumable_id}",        _admin_only(), None,                         "Delete consumable"),
    ("POST",   "/api/consumables/{consumable_id}/adjust", _op_write(),   {"quantity": 1},              "Adjust consumable stock"),

    # =========================================================================
    # 45. Education, Export & Reports
    # =========================================================================
    ("GET", "/api/education/usage-report", _op_write(),   None, "Education usage report (operator+)"),
    ("GET", "/api/export/audit-logs",      _admin_read(), None, "Export audit logs"),
    ("GET", "/api/reports/chargebacks",    _admin_read(), None, "Chargeback report"),

    # =========================================================================
    # 46. Health & Metrics (API-prefixed)
    # =========================================================================
    ("GET", "/api/health",  _pub(),      None, "API health check"),
    ("GET", "/api/metrics", _api_read(), None, "API metrics endpoint"),

    # =========================================================================
    # 47. HMS codes — template form
    # =========================================================================
    ("GET", "/api/hms-codes/{code}", _api_read(), None, "HMS code lookup (template)"),

    # =========================================================================
    # 48. Jobs — missing actions
    # =========================================================================
    ("POST", "/api/jobs/bulk-update",       _op_write(), {"job_ids": [], "status": "pending"}, "Bulk update jobs"),
    ("POST", "/api/jobs/{job_id}/approve",  _op_write(), None, "Approve job"),
    ("POST", "/api/jobs/{job_id}/dispatch", _op_write(), None, "Dispatch job"),
    ("POST", "/api/jobs/{job_id}/reject",   _op_write(), None, "Reject job"),
    ("POST", "/api/jobs/{job_id}/resubmit", _api_read(), None, "Resubmit job"),

    # =========================================================================
    # 49. License — missing endpoints
    # =========================================================================
    ("GET",  "/api/license/installation-id",    _admin_read(), None, "Get installation ID"),
    ("POST", "/api/license/activation-request", _admin_only(), {"key": "x", "nonce": "x"}, "Create signed offline activation request"),
    ("POST", "/api/license/unactivation-request", _admin_only(), {"nonce": "x"}, "Create signed offline unactivation request"),
    ("POST", "/api/license/activate",           _admin_only(), {},   "Activate license online"),
    ("POST", "/api/license/upload",             _admin_only(), None, "Upload license file"),
    # Online unactivation/reactivation call the license portal. Test servers point
    # LICENSE_SERVER_URL at a closed loopback port (no test reaches runsodin.com): a licensed
    # server answers 502 "Could not reach license server"; Community has no license key: 400.
    ("POST", "/api/license/unactivate",         _ONLINE_LICENSE, None, "Unactivate license online"),
    ("POST", "/api/license/reactivate",         _ONLINE_LICENSE, None, "Reactivate current license"),

    # =========================================================================
    # 50. Models — missing actions
    # =========================================================================
    ("GET",    "/api/models/{model_id}/revisions",                     _api_read(), None, "List model revisions"),
    ("POST",   "/api/models/{model_id}/revisions",                     _op_write(), None, "Create model revision"),
    ("POST",   "/api/models/{model_id}/revisions/{rev_number}/revert", _op_write(), None, "Revert to revision"),
    ("POST",   "/api/models/{model_id}/schedule",                      _op_write(), {},   "Schedule model"),
    # models.delete defaults to admin: CHANGELOG 2.0.0 "Operators lose API-only access to ... delete models".
    ("DELETE", "/api/models/{model_id}/variants/{variant_id}",         _admin_only(), None, "Delete model variant — models.delete (admin by default)"),

    # =========================================================================
    # 51. Orders — missing sub-resources
    # =========================================================================
    ("DELETE", "/api/orders/{order_id}/items/{item_id}",  _op_write(),   None, "Remove order item"),
    ("PATCH",  "/api/orders/{order_id}/items/{item_id}",  _admin_only(), {},   "Update order item"),
    ("GET",    "/api/orders/{order_id}/invoice.pdf",      _op_write(),   None, "Download invoice PDF"),

    # =========================================================================
    # 52. Presets
    # =========================================================================
    ("GET",    "/api/presets",                      _api_read(), None,                     "List presets"),
    ("POST",   "/api/presets",                      _op_write(), None,  "Create preset"),
    ("DELETE", "/api/presets/{preset_id}",          _op_write(), None,                     "Delete preset"),
    # Scheduling a preset creates a job: jobs.create (admin, operator by default; FEATURES.md §11.3 (Permission matrix)).
    ("POST",   "/api/presets/{preset_id}/schedule", _op_write(), {},                       "Schedule preset — jobs.create"),

    # =========================================================================
    # 53. Print Files — missing actions / template forms
    # =========================================================================
    ("GET",    "/api/print-files/{file_id}",          _api_read(), None, "Get print file (template)"),
    ("GET",    "/api/print-files/{file_id}/mesh",     _api_read(), None, "Get print file mesh"),
    ("DELETE", "/api/print-files/{file_id}",          _op_write(), None, "Delete print file (template)"),
    ("POST",   "/api/print-files/upload",             _op_write(), None, "Upload print file"),
    ("POST",   "/api/print-files/{file_id}/schedule", _op_write(), {},   "Schedule print file"),

    # =========================================================================
    # 54. Printers — missing sub-resources / template slot forms
    # =========================================================================
    ("GET",    "/api/printers/tags",                                         _api_read(),   None, "List printer tags"),
    ("GET",    "/api/printers/{printer_id}/hms-history",                     _api_read(),   None, "HMS history"),
    ("GET",    "/api/printers/{printer_id}/nozzle-status",                   _api_read(),   None, "Nozzle status (H2D dual-nozzle aware)"),
    ("GET",    "/api/printers/{printer_id}/nozzle",                          _api_read(),   None, "Get current nozzle"),
    ("GET",    "/api/printers/{printer_id}/nozzle/history",                  _api_read(),   None, "Nozzle history"),
    ("POST",   "/api/printers/{printer_id}/nozzle",                          _op_write(),   {},   "Install nozzle"),
    ("PATCH",  "/api/printers/{printer_id}/nozzle/{nozzle_id}/retire",       _op_write(),   None, "Retire nozzle"),
    ("GET",    "/api/printers/{printer_id}/telemetry",                       _api_read(),   None, "Printer telemetry"),
    ("GET",    "/api/printers/{printer_id}/vision",                          _api_read(),   None, "Printer vision settings"),
    ("PATCH",  "/api/printers/{printer_id}/vision",                          _admin_only(), {},   "Update printer vision settings"),
    ("POST",   "/api/printers/bulk-update",                                  _admin_only(), {"printer_ids": [], "action": "enable"}, "Bulk update printers"),
    ("PATCH",  "/api/printers/{printer_id}/slots/{slot_number}",             _op_write(),   {"filament_type": "PLA"},                 "Update slot (template)"),
    ("PATCH",  "/api/printers/{printer_id}/slots/{slot_number}/manual-assign", _op_write(), {"filament_type": "PLA", "color": "Red"}, "Manual assign (template)"),
    ("POST",   "/api/printers/{printer_id}/slots/{slot_number}/assign",      _op_write(),   None, "Assign spool (template)"),
    ("POST",   "/api/printers/{printer_id}/slots/{slot_number}/confirm",     _op_write(),   None, "Confirm spool assign (template)"),

    # =========================================================================
    # 55. Products — missing sub-resources
    # =========================================================================
    ("POST",   "/api/products/{product_id}/consumables",                      _op_write(), {"consumable_id": 1}, "Add product consumable"),
    ("DELETE", "/api/products/{product_id}/consumables/{consumable_link_id}", _op_write(), None, "Remove product consumable"),
    ("DELETE", "/api/products/{product_id}/components/{component_id}",        _op_write(), None, "Remove BOM component"),

    # =========================================================================
    # 56. Report Schedules
    # =========================================================================
    ("GET",    "/api/report-schedules",                    _admin_read(), None, "List report schedules"),
    ("POST",   "/api/report-schedules",                    _admin_only(), {},   "Create report schedule"),
    ("PATCH",  "/api/report-schedules/{schedule_id}",      _admin_only(), {},   "Update report schedule"),
    ("DELETE", "/api/report-schedules/{schedule_id}",      _admin_only(), None, "Delete report schedule"),
    ("POST",   "/api/report-schedules/{schedule_id}/run",  _admin_only(), None, "Run report schedule immediately"),

    # =========================================================================
    # 57. Search — plain path (existing entry has ?q=test suffix which doesn't match)
    # =========================================================================
    ("GET", "/api/search", _api_read(), None, "Global search (template)"),

    # =========================================================================
    # 58. Settings — education mode
    # =========================================================================
    ("GET", "/api/settings/education-mode", _pub(),        None,               "Get education mode"),
    ("PUT", "/api/settings/education-mode", _admin_only(), {"enabled": False},  "Set education mode"),

    # =========================================================================
    # 59. Setup — missing endpoints
    # =========================================================================
    ("GET",  "/api/setup/network",  _api_read(),    None, "Get network config"),
    ("POST", "/api/setup/network",  _setup_locked(), None, "Save network config (locked after setup)"),

    # =========================================================================
    # 60. Spools — missing sub-resources / template forms
    # =========================================================================
    ("GET",  "/api/spools/labels/batch",              _api_read(), None, "Batch spool labels (template)"),
    ("GET",  "/api/spools/lookup/{qr_code}",          _api_read(), None, "QR code lookup (template)"),
    ("GET",  "/api/spools/{spool_id}/drying-history", _api_read(), None, "Spool drying history"),
    ("POST", "/api/spools/bulk-update",               _op_write(), {},   "Bulk update spools"),
    ("POST", "/api/spools/{spool_id}/dry",            _op_write(), {},   "Start spool drying"),

    # =========================================================================
    # 61. Timelapses
    # =========================================================================
    ("GET",    "/api/timelapses",                        _api_read(),   None, "List timelapses"),
    ("GET",    "/api/timelapses/{timelapse_id}",        _api_read(),   None, "Get timelapse"),
    ("GET",    "/api/timelapses/{timelapse_id}/video",  _api_read(),   None, "Download timelapse video"),
    ("GET",    "/api/timelapses/{timelapse_id}/stream", _api_read(),   None, "Stream timelapse for playback"),
    ("GET",    "/api/timelapses/{timelapse_id}/download", _api_read(), None, "Download timelapse as attachment"),
    # Edits and deletion are conditional on the exact recording/lifetime (If-Match, sent by the web
    # client: frontend/src/api/cameras.ts). Authorized callers without If-Match get 428 after the
    # role check (certified: test_timelapse_edit_http.py missing_etag -> 428; test_timelapse_route_activation.py).
    ("POST",   "/api/timelapses/{timelapse_id}/trim",   {**_op_write(), "operator": 428, "admin": 428},
     {"start_seconds": 0, "end_seconds": 10}, "Trim timelapse (operator+, If-Match required)"),
    ("POST",   "/api/timelapses/{timelapse_id}/speed",  {**_op_write(), "operator": 428, "admin": 428},
     {"multiplier": 2.0}, "Speed-adjust timelapse (operator+, If-Match required)"),
    ("DELETE", "/api/timelapses/{timelapse_id}",        {**_admin_only(), "admin": 428}, None, "Delete timelapse (admin, If-Match required)"),

    # =========================================================================
    # 62. Vision
    # =========================================================================
    ("GET",    "/api/vision/detections",                         _api_read(),   None, "List detections"),
    ("GET",    "/api/vision/detections/{detection_id}",          _api_read(),   None, "Get detection"),
    ("PATCH",  "/api/vision/detections/{detection_id}",          _op_write(), {"status": "confirmed"},   "Update detection (operator+)"),
    ("GET",    "/api/vision/frames/{printer_id}/{filename}",     _api_read(),   None, "Get vision frame"),
    ("GET",    "/api/vision/frames/{printer_id}/training/{filename}", _api_read(), None, "Get vision training frame"),
    ("GET",    "/api/vision/models",                             _admin_read(), None, "List vision models"),
    ("POST",   "/api/vision/models",                             _admin_only(), None, "Upload vision model"),
    ("PATCH",  "/api/vision/models/{model_id}/activate",         _admin_only(), None, "Activate vision model"),
    ("DELETE", "/api/vision/models/{model_id}",                  _admin_only(), None, "Delete vision model"),
    ("GET",    "/api/vision/settings",                           _admin_read(), None, "Get vision settings (admin only)"),
    ("PATCH",  "/api/vision/settings",                           _admin_only(), {},   "Update vision settings"),
    ("GET",    "/api/vision/stats",                              _api_read(),   None, "Vision stats"),
    ("GET",    "/api/vision/training-data",                      _admin_read(), None, "List vision training data"),
    ("GET",    "/api/vision/training-data/export",               _admin_read(), None, "Export vision training data"),
    ("POST",   "/api/vision/training-data/{detection_id}/label", _admin_only(), {},   "Label detection for training"),

    # =========================================================================
    # 63. Profiles
    # =========================================================================
    ("GET",    "/api/profiles",                       _api_read(),   None, "List profiles"),
    ("POST",   "/api/profiles",                       _op_write(),   {"name": "test", "slicer": "klipper", "category": "temperature", "raw_content": "{}"}, "Create profile (operator+)"),
    ("POST",   "/api/profiles/import",                _op_write(),   None, "Import profile file (operator+)"),
    ("GET",    "/api/profiles/{profile_id}",          _api_read(),   None, "Get profile"),
    # The fixture profile is the admin's; operators edit only their own (certified:
    # test_profile_history_behavior.py::test_operator_cannot_mutate_same_org_other_creator).
    ("PUT",    "/api/profiles/{profile_id}",          {**_op_write(), "operator": 403}, {"name": "updated"}, "Update profile (own, or admin)"),
    ("DELETE", "/api/profiles/{profile_id}",          _op_write(),   None, "Delete profile (operator+)"),
    ("GET",    "/api/profiles/{profile_id}/export",   _api_read(),   None, "Export profile download"),
    ("POST",   "/api/profiles/{profile_id}/apply",    _op_write(),   {"printer_id": 1}, "Apply Klipper profile (operator+)"),

    # =========================================================================
    # 64. Analytics
    # =========================================================================
    ("GET", "/api/analytics/failures",      _api_read(), None, "Failure analytics"),
    ("GET", "/api/analytics/time-accuracy", _api_read(), None, "Time accuracy analytics"),
]
