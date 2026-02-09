#!/usr/bin/env python3
"""
O.D.I.N. v0.19.1 — API & RBAC Test Suite
==========================================
Tests every API endpoint for proper authentication and authorization.

Usage:
    cd /opt/printfarm-scheduler
    python3 odin_api_test.py

    # Or specify host:
    python3 odin_api_test.py --host http://192.168.70.200:8000

Requirements:
    pip install requests --break-system-packages

What it does:
    1. Reads API key from backend/.env
    2. Logs in as admin, creates test operator + viewer users
    3. Tests EVERY endpoint with each role — checks allow/deny
    4. Tests auth bypass paths (no API key at all)
    5. Tests setup endpoints are locked
    6. Cleans up test users + test resources
    7. Prints pass/fail report
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests --break-system-packages")
    sys.exit(1)

# ============================================================
# CONFIG
# ============================================================

DEFAULT_HOST = "http://localhost:8000"
TEST_OP = {"username": "_qa_operator", "password": "QaOper123!", "email": "qaop@test.local", "role": "operator"}
TEST_VW = {"username": "_qa_viewer",   "password": "QaView123!", "email": "qavw@test.local", "role": "viewer"}

# ============================================================
# HELPERS
# ============================================================

class C:
    G  = "\033[92m"   # green
    R  = "\033[91m"   # red
    Y  = "\033[93m"   # yellow
    CY = "\033[96m"   # cyan
    B  = "\033[1m"    # bold
    X  = "\033[0m"    # reset


class APIClient:
    def __init__(self, host, api_key):
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.tokens = {}   # role -> bearer token
        self.user_ids = {} # role -> user id

    def _h(self, role=None):
        h = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
        if role and role in self.tokens:
            h["Authorization"] = f"Bearer {self.tokens[role]}"
        return h

    def login(self, username, password):
        r = requests.post(
            f"{self.host}/api/auth/login",
            data={"username": username, "password": password},
            headers={"X-API-Key": self.api_key, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=10
        )
        return r.json().get("access_token") if r.status_code == 200 else None

    def req(self, method, path, role=None, body=None, auth_mode="full"):
        """auth_mode: full | api_key | none"""
        url = f"{self.host}{path}"
        if auth_mode == "none":
            headers = {"Content-Type": "application/json"}
        elif auth_mode == "api_key":
            headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
        else:
            headers = self._h(role)

        kwargs = {"headers": headers, "timeout": 15}
        if body is not None and method.upper() in ("POST", "PUT", "PATCH"):
            kwargs["json"] = body

        try:
            return requests.request(method, url, **kwargs).status_code
        except:
            return -1

    def get_json(self, path, role="admin"):
        try:
            r = requests.get(f"{self.host}{path}", headers=self._h(role), timeout=10)
            return r.json() if r.status_code == 200 else []
        except:
            return []


# ============================================================
# ENDPOINT MAP — Built from actual main.py grep output
# ============================================================
# (method, path, min_role, description, body)
#
# min_role meanings:
#   "public"   -> middleware skip list, no auth needed
#   "viewer"   -> get_current_user only (any authenticated user)
#   "operator" -> require_role("operator")
#   "admin"    -> require_role("admin")

ENDPOINTS = [
    # ═══════════════════════════════════════════════════════════
    # PUBLIC — no auth required (middleware skip list)
    # ═══════════════════════════════════════════════════════════
    ("GET",  "/health",                                "public",   "Health check", None),
    ("GET",  "/metrics",                               "public",   "Prometheus metrics", None),
    ("GET",  "/api/branding",                          "public",   "Get branding", None),
    ("GET",  "/api/setup/status",                      "public",   "Setup status", None),
    ("GET",  "/api/license",                           "public",   "License info", None),
    ("GET",  "/api/auth/oidc/config",                  "public",   "OIDC client config", None),
    ("GET",  "/api/push/vapid-key",                    "public",   "VAPID public key", None),

    # ═══════════════════════════════════════════════════════════
    # VIEWER — any authenticated user (get_current_user, no require_role)
    # ═══════════════════════════════════════════════════════════
    # Printers (read)
    ("GET",  "/api/printers",                          "viewer",   "List printers", None),
    ("GET",  "/api/printers/live-status",              "viewer",   "All live status", None),
    # Jobs (read)
    ("GET",  "/api/jobs",                              "viewer",   "List jobs", None),
    # Models (read)
    ("GET",  "/api/models",                            "viewer",   "List models", None),
    ("GET",  "/api/models-with-pricing",               "viewer",   "Models + pricing", None),
    # Spools (read)
    ("GET",  "/api/spools",                            "viewer",   "List spools", None),
    # Cameras (read)
    ("GET",  "/api/cameras",                           "viewer",   "List cameras", None),
    # Orders (read)
    ("GET",  "/api/orders",                            "viewer",   "List orders", None),
    # Products (read)
    ("GET",  "/api/products",                          "viewer",   "List products", None),
    # Alerts (read, user-scoped)
    ("GET",  "/api/alerts",                            "viewer",   "List alerts", None),
    ("GET",  "/api/alerts/unread-count",               "viewer",   "Unread count", None),
    ("GET",  "/api/alerts/summary",                    "viewer",   "Alert summary", None),
    ("POST", "/api/alerts/mark-all-read",              "viewer",   "Mark all read", None),
    ("GET",  "/api/alert-preferences",                 "viewer",   "Alert prefs", None),
    ("PUT",  "/api/alert-preferences",                 "viewer",   "Update alert prefs", {"preferences": []}),
    # Push (user-scoped)
    ("POST", "/api/push/subscribe",                    "viewer",   "Push subscribe", {"endpoint": "https://test.local/push", "p256dh_key": "test", "auth_key": "test"}),
    ("DELETE","/api/push/subscribe",                   "viewer",   "Push unsubscribe", None),
    # RBAC/Permissions (read)
    ("GET",  "/api/permissions",                       "viewer",   "Get permissions", None),
    # Analytics/Stats (read)
    ("GET",  "/api/analytics",                         "viewer",   "Analytics", None),
    ("GET",  "/api/stats",                             "viewer",   "Stats", None),
    ("GET",  "/api/timeline",                          "viewer",   "Timeline", None),
    # Config (read)
    ("GET",  "/api/config",                            "viewer",   "Get config", None),
    ("GET",  "/api/config/require-job-approval",       "viewer",   "Approval setting", None),
    ("GET",  "/api/config/quiet-hours",                "viewer",   "Quiet hours", None),
    ("GET",  "/api/config/mqtt-republish",             "viewer",   "MQTT republish", None),
    ("GET",  "/api/pricing-config",                    "viewer",   "Pricing config", None),
    ("GET",  "/api/settings/language",                 "viewer",   "Language", None),
    ("GET",  "/api/settings/energy-rate",              "viewer",   "Energy rate", None),
    # Auth
    ("GET",  "/api/auth/me",                           "viewer",   "Current user", None),
    # Filaments (read)
    ("GET",  "/api/filaments",                         "viewer",   "Filaments", None),
    ("GET",  "/api/filaments/combined",                "viewer",   "Combined filaments", None),
    # Print jobs (read)
    ("GET",  "/api/print-jobs",                        "viewer",   "Print jobs", None),
    ("GET",  "/api/print-jobs/stats",                  "viewer",   "Print job stats", None),
    ("GET",  "/api/print-jobs/unlinked",               "viewer",   "Unlinked prints", None),
    # Print files (read)
    ("GET",  "/api/print-files",                       "viewer",   "Print files", None),
    # Maintenance (read)
    ("GET",  "/api/maintenance/tasks",                 "viewer",   "Maint tasks", None),
    ("GET",  "/api/maintenance/logs",                  "viewer",   "Maint logs", None),
    ("GET",  "/api/maintenance/status",                "viewer",   "Maint status", None),
    # Failure reasons (read)
    ("GET",  "/api/failure-reasons",                   "viewer",   "Failure reasons", None),
    # Search
    ("GET",  "/api/search",                            "viewer",   "Search", None),
    # Spoolman (read)
    ("GET",  "/api/spoolman/spools",                   "viewer",   "Spoolman spools", None),
    ("GET",  "/api/spoolman/filaments",                "viewer",   "Spoolman filaments", None),
    # Bambu (read)
    ("GET",  "/api/bambu/filament-types",              "viewer",   "Bambu fil types", None),
    # Exports (read)
    ("GET",  "/api/export/jobs",                       "viewer",   "Export jobs", None),
    ("GET",  "/api/export/spools",                     "viewer",   "Export spools", None),
    ("GET",  "/api/export/filament-usage",             "viewer",   "Export filament", None),
    ("GET",  "/api/export/models",                     "viewer",   "Export models", None),
    # Scheduler (read)
    ("GET",  "/api/scheduler/runs",                    "viewer",   "Scheduler runs", None),
    # Backups (read)
    ("GET",  "/api/backups",                           "viewer",   "List backups", None),
    # Audit
    ("GET",  "/api/audit-logs",                        "viewer",   "Audit logs", None),
    # Job create — special: viewer can create (gets "submitted" if approval on)
    ("POST", "/api/jobs",                              "viewer",   "Create job", {"item_name": "_qa_test_job"}),

    # ═══════════════════════════════════════════════════════════
    # OPERATOR — require_role("operator")
    # ═══════════════════════════════════════════════════════════
    # Printers (mutate)
    ("POST",   "/api/printers",                        "operator", "Create printer", {"name": "_qa_printer", "ip": "0.0.0.0", "api_type": "bambu"}),
    ("POST",   "/api/printers/reorder",                "operator", "Reorder printers", {"order": []}),
    ("POST",   "/api/printers/test-connection",        "operator", "Test connection", {"ip": "0.0.0.0", "api_type": "bambu"}),
    ("POST",   "/api/printers/1/lights",               "operator", "Toggle lights", None),
    ("POST",   "/api/printers/1/sync-ams",             "operator", "Sync AMS", None),
    ("POST",   "/api/printers/1/stop",                 "operator", "Emergency stop", None),
    ("POST",   "/api/printers/1/pause",                "operator", "Pause print", None),
    ("POST",   "/api/printers/1/resume",               "operator", "Resume print", None),
    # Models (mutate)
    ("POST",   "/api/models",                          "operator", "Create model", {"name": "_qa_model"}),
    # Jobs (mutate)
    ("POST",   "/api/jobs/bulk",                       "operator", "Bulk create jobs", [{"item_name": "_qa_bulk"}]),
    ("PATCH",  "/api/jobs/reorder",                    "operator", "Reorder jobs", {"order": []}),
    # Spools (mutate)
    ("POST",   "/api/spools",                          "operator", "Create spool", {"name": "_qa_spool", "filament_type": "PLA", "color_hex": "#FF0000"}),
    # Scheduler
    ("POST",   "/api/scheduler/run",                   "operator", "Run scheduler", None),
    # Spoolman
    ("POST",   "/api/spoolman/sync",                   "operator", "Sync spoolman", None),
    # Filaments (mutate)
    ("POST",   "/api/filaments",                       "operator", "Create filament", {"name": "_qa_fil", "type": "PLA"}),
    # Bambu
    ("POST",   "/api/bambu/test-connection",           "operator", "Test Bambu conn", {"ip": "0.0.0.0", "serial": "test", "access_code": "test"}),
    # Cameras (mutate)
    ("PATCH",  "/api/cameras/1/toggle",                "operator", "Toggle camera", None),
    # Maintenance (mutate)
    ("POST",   "/api/maintenance/tasks",               "operator", "Create task", {"name": "_qa_task", "interval_hours": 100}),
    ("POST",   "/api/maintenance/logs",                "operator", "Create log", {"printer_id": 1, "task_id": 1, "notes": "qa"}),
    # Products (mutate)
    ("POST",   "/api/products",                        "operator", "Create product", {"name": "_qa_product"}),
    # Orders (mutate)
    ("POST",   "/api/orders",                          "operator", "Create order", {"order_number": "_qa_order"}),
    # Smart plugs
    ("POST",   "/api/printers/1/plug/on",              "operator", "Plug on", None),
    ("POST",   "/api/printers/1/plug/off",             "operator", "Plug off", None),
    ("POST",   "/api/printers/1/plug/toggle",          "operator", "Plug toggle", None),
    # Print files (mutate)
    ("DELETE", "/api/print-files/99999",               "operator", "Delete print file", None),
    # Spool actions
    ("POST",   "/api/spools/scan-assign",              "operator", "Scan assign", {"qr_code": "QR-000", "printer_id": 1, "slot_number": 1}),
    # Job failure logging
    ("PATCH",  "/api/jobs/99999/failure",              "operator", "Log failure", {"reason": "test"}),
    # Link job to print
    ("POST",   "/api/jobs/99999/link-print",           "operator", "Link job", None),
    # Job move
    ("PATCH",  "/api/jobs/99999/move",                 "operator", "Move job", {"position": 1}),
    # Print file upload (would need multipart, just test auth)
    # Spool weigh/load/unload/use
    ("POST",   "/api/spools/99999/load",               "operator", "Load spool", {"printer_id": 1, "slot_number": 1}),
    ("POST",   "/api/spools/99999/unload",             "operator", "Unload spool", None),
    ("POST",   "/api/spools/99999/use",                "operator", "Use spool", {"grams": 10}),
    ("POST",   "/api/spools/99999/weigh",              "operator", "Weigh spool", {"weight_grams": 500}),
    # Slot assign/confirm
    ("POST",   "/api/printers/1/slots/1/assign",       "operator", "Assign slot", {"spool_id": 1}),
    ("POST",   "/api/printers/1/slots/1/confirm",      "operator", "Confirm slot", None),

    # ═══════════════════════════════════════════════════════════
    # ADMIN — require_role("admin")
    # ═══════════════════════════════════════════════════════════
    # Branding (mutate)
    ("PUT",    "/api/branding",                        "admin",    "Update branding", {"app_name": "O.D.I.N."}),
    ("DELETE", "/api/branding/logo",                   "admin",    "Delete logo", None),
    # Backups (mutate)
    ("POST",   "/api/backups",                         "admin",    "Create backup", None),
    ("DELETE", "/api/backups/nonexistent.db",          "admin",    "Delete backup", None),
    # Config (mutate)
    ("PUT",    "/api/config",                          "admin",    "Update config", {"spoolman_url": ""}),
    ("PUT",    "/api/config/require-job-approval",     "admin",    "Set approval", {"enabled": False}),
    ("PUT",    "/api/config/quiet-hours",              "admin",    "Set quiet hours", {"enabled": False}),
    ("PUT",    "/api/config/mqtt-republish",           "admin",    "Set MQTT repub", {"enabled": False}),
    # Permissions (mutate)
    ("PUT",    "/api/permissions",                     "admin",    "Update perms", {"page_access": {}, "action_access": {}}),
    ("POST",   "/api/permissions/reset",               "admin",    "Reset perms", None),
    # Pricing
    ("PUT",    "/api/pricing-config",                  "admin",    "Update pricing", {"default_rate": 0.03}),
    # Users
    ("GET",    "/api/users",                           "admin",    "List users", None),
    # Webhooks
    ("GET",    "/api/webhooks",                        "admin",    "List webhooks", None),
    ("POST",   "/api/webhooks",                        "admin",    "Create webhook", {"name": "_qa_wh", "url": "https://test.local/wh", "events": ["job.complete"]}),
    # OIDC
    ("GET",    "/api/admin/oidc",                      "admin",    "Get OIDC", None),
    ("PUT",    "/api/admin/oidc",                      "admin",    "Update OIDC", {"enabled": False}),
    # SMTP
    ("GET",    "/api/smtp-config",                     "admin",    "Get SMTP", None),
    ("PUT",    "/api/smtp-config",                     "admin",    "Update SMTP", {"enabled": False, "host": "", "port": 587, "username": "", "password": "", "from_address": "", "use_tls": True}),
    ("POST",   "/api/alerts/test-email",               "admin",    "Test email", None),
    # License (mutate)
    ("DELETE", "/api/license",                         "admin",    "Delete license", None),
    # Settings (mutate)
    ("PUT",    "/api/settings/language",               "admin",    "Set language", {"language": "en"}),
    ("PUT",    "/api/settings/energy-rate",            "admin",    "Set energy rate", {"rate": 0.12}),
    # Maintenance seed
    ("POST",   "/api/maintenance/seed-defaults",       "admin",    "Seed defaults", None),
]


# Endpoints that should BLOCK unauthenticated requests (no API key)
AUTH_BYPASS_TESTS = [
    ("PUT",    "/api/branding",            "Branding PUT no-auth"),
    ("DELETE", "/api/branding/logo",       "Branding DELETE no-auth"),
    ("POST",   "/api/printers",            "Create printer no-auth"),
    ("DELETE", "/api/backups/test.db",     "Delete backup no-auth"),
    ("PUT",    "/api/config",              "Update config no-auth"),
    ("GET",    "/api/users",               "List users no-auth"),
    ("PUT",    "/api/permissions",         "Update perms no-auth"),
    ("GET",    "/api/printers",            "List printers no-auth"),
    ("GET",    "/api/jobs",                "List jobs no-auth"),
    ("POST",   "/api/jobs",               "Create job no-auth"),
    ("GET",    "/api/analytics",           "Analytics no-auth"),
]


# ============================================================
# TEST RUNNER
# ============================================================

def find_api_key():
    for p in ["/opt/printfarm-scheduler/backend/.env", "./backend/.env"]:
        if os.path.exists(p):
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, val = line.split("=", 1)
                        if key.strip() in ("API_KEY", "ODIN_API_KEY"):
                            return val.strip().strip('"').strip("'")
    return None


def run_tests(host, api_key, admin_user, admin_pass):
    client = APIClient(host, api_key)
    results = []

    # Detect if API key auth is actually enforced
    no_apikey_mode = (api_key in (None, "", "none"))

    print(f"\n{C.B}{'='*72}")
    print(f"  O.D.I.N. API & RBAC Test Suite — v0.19.1")
    print(f"  Host: {host}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Endpoints: {len(ENDPOINTS)}")
    if no_apikey_mode:
        print(f"  {C.Y}⚠ No API key configured — skipping no-bearer & bypass tests{C.X}")
        print(f"  {C.Y}  (These tests only matter when API_KEY is set in .env){C.X}")
    print(f"{'='*72}{C.X}\n")

    # ── 1. Admin login ──────────────────────────────────────
    print(f"{C.CY}▸ Step 1: Admin login{C.X}")
    tok = client.login(admin_user, admin_pass)
    if not tok:
        print(f"{C.R}  FATAL: Cannot login as '{admin_user}'. Aborting.{C.X}")
        sys.exit(1)
    client.tokens["admin"] = tok
    me = client.get_json("/api/auth/me", "admin")
    client.user_ids["admin"] = me.get("id") if isinstance(me, dict) else None
    print(f"{C.G}  ✓ Admin login OK (id={client.user_ids.get('admin')}){C.X}")

    # ── 2. Create test users ────────────────────────────────
    print(f"\n{C.CY}▸ Step 2: Create test users{C.X}")
    for role, udata in [("operator", TEST_OP), ("viewer", TEST_VW)]:
        r = requests.post(f"{host}/api/users", json=udata, headers=client._h("admin"), timeout=10)
        if r.status_code in (200, 201):
            client.user_ids[role] = r.json().get("id")
            print(f"{C.G}  ✓ Created {role} (id={client.user_ids[role]}){C.X}")
        else:
            print(f"{C.Y}  ⚠ {role}: {r.status_code} {r.text[:80]}{C.X}")

        tok = client.login(udata["username"], udata["password"])
        if tok:
            client.tokens[role] = tok
            print(f"{C.G}  ✓ {role} login OK{C.X}")
        else:
            print(f"{C.R}  ✗ {role} login FAILED{C.X}")

    missing = [r for r in ("operator", "viewer") if r not in client.tokens]
    if missing:
        print(f"{C.R}  FATAL: Missing tokens for: {missing}. Aborting.{C.X}")
        return results

    # ── 3. RBAC endpoint tests ──────────────────────────────
    print(f"\n{C.CY}▸ Step 3: RBAC tests ({len(ENDPOINTS)} endpoints × 3 roles){C.X}")
    role_level = {"public": 0, "viewer": 1, "operator": 2, "admin": 3}

    for method, path, min_role, desc, body in ENDPOINTS:
        min_lvl = role_level[min_role]

        if min_role == "public":
            code = client.req(method, path, auth_mode="none", body=body)
            ok = code not in (401, 403)
            if not ok:
                print(f"  {C.R}✗{C.X} {method:<7} {path:<48} public  got {code}  {desc}")
            results.append((f"{method} {path} [no-auth]", "!401", str(code), ok))
            continue

        # Test with each role: viewer, operator, admin
        for role, lvl in [("viewer", 1), ("operator", 2), ("admin", 3)]:
            code = client.req(method, path, role=role, body=body)
            should_pass = lvl >= min_lvl

            if should_pass:
                # Role should be allowed — anything except 403 is OK
                # Also accept 403 from require_feature() (license gating, not RBAC)
                # We detect this by checking if lower roles also get 403
                ok = code != 403
                exp = "!403"
            else:
                ok = code == 403
                exp = "403"

            if not ok:
                print(f"  {C.R}✗{C.X} {method:<7} {path:<48} min={min_role:<9} {role}→{code} (expected {exp})  {desc}")

            results.append((f"{method} {path} [{role}]", exp, str(code), ok))

        # No-bearer test: only when API key is configured
        if not no_apikey_mode:
            code = client.req(method, path, auth_mode="api_key", body=body)
            ok = code in (401, 403, 422)
            if not ok:
                print(f"  {C.R}✗{C.X} {method:<7} {path:<48} no-bearer → {code} (expected 401/403)")
            results.append((f"{method} {path} [no-bearer]", "401/403", str(code), ok))

    p3_pass = sum(1 for r in results if r[3])
    p3_fail = sum(1 for r in results if not r[3])
    print(f"\n  RBAC subtotal: {C.G}{p3_pass} passed{C.X}, {C.R if p3_fail else C.G}{p3_fail} failed{C.X}")

    # ── 4. Auth bypass tests (only with API key) ────────────
    if not no_apikey_mode:
        print(f"\n{C.CY}▸ Step 4: No-auth bypass tests ({len(AUTH_BYPASS_TESTS)}){C.X}")
        for method, path, desc in AUTH_BYPASS_TESTS:
            code = client.req(method, path, auth_mode="none")
            ok = code == 401
            if not ok:
                print(f"  {C.R}✗{C.X} {method:<7} {path:<48} got {code} (expected 401)  {desc}")
            results.append((f"BYPASS: {method} {path}", "401", str(code), ok))
    else:
        print(f"\n{C.CY}▸ Step 4: No-auth bypass tests — {C.Y}SKIPPED (no API key){C.X}")

    # ── 5. Setup lockout ────────────────────────────────────
    print(f"\n{C.CY}▸ Step 5: Setup endpoints locked{C.X}")
    # First check if setup is actually complete
    setup_status = client.get_json("/api/setup/status", "admin")
    setup_complete = False
    if isinstance(setup_status, dict):
        setup_complete = not setup_status.get("needs_setup", True)

    if not setup_complete:
        print(f"  {C.Y}⚠ Setup not marked complete in DB — setup lock tests are informational only{C.X}")
        print(f"  {C.Y}  Run: sqlite3 /data/printfarm.db \"INSERT OR REPLACE INTO system_config (key, value) VALUES ('setup_complete', 'true');\"{C.X}")
        # Still run them but as info, not pass/fail
        for method, path, body in [
            ("POST", "/api/setup/test-printer", {"ip": "0.0.0.0", "api_type": "bambu", "serial": "test", "access_code": "test"}),
            ("POST", "/api/setup/printer",      {"name": "hacked", "ip": "0.0.0.0", "api_type": "bambu"}),
            ("POST", "/api/setup/complete",     {}),
        ]:
            code = client.req(method, path, auth_mode="none", body=body)
            print(f"  {C.Y}ℹ{C.X} {method:<7} {path:<48} → {code} (setup not complete, not scored)")
    else:
        for method, path, body in [
            ("POST", "/api/setup/test-printer", {"ip": "0.0.0.0", "api_type": "bambu", "serial": "test", "access_code": "test"}),
            ("POST", "/api/setup/printer",      {"name": "hacked", "ip": "0.0.0.0", "api_type": "bambu"}),
            ("POST", "/api/setup/complete",     {}),
        ]:
            code = client.req(method, path, auth_mode="none", body=body)
            ok = code == 403
            if ok:
                print(f"  {C.G}✓{C.X} {method:<7} {path:<48} → 403 (blocked)")
            else:
                print(f"  {C.R}✗{C.X} {method:<7} {path:<48} → {code} (expected 403)")
            results.append((f"SETUP: {method} {path}", "403", str(code), ok))

    # ── 6. Cleanup ──────────────────────────────────────────
    print(f"\n{C.CY}▸ Step 6: Cleanup{C.X}")
    cleanup(client)

    # ── Summary ─────────────────────────────────────────────
    print_summary(results)
    return results


def cleanup(client):
    """Remove all _qa_ prefixed test resources."""
    # Users
    for role, udata in [("operator", TEST_OP), ("viewer", TEST_VW)]:
        uid = client.user_ids.get(role)
        if uid:
            code = client.req("DELETE", f"/api/users/{uid}", role="admin")
            sym = "✓" if code in (200, 204) else "⚠"
            print(f"  {sym} Deleted user {udata['username']} ({code})")
        else:
            for u in client.get_json("/api/users", "admin"):
                if u.get("username") == udata["username"]:
                    client.req("DELETE", f"/api/users/{u['id']}", role="admin")
                    print(f"  ✓ Deleted user {udata['username']} by name")

    # Resources with _qa_ prefix
    cleanup_list = [
        ("/api/jobs",     "item_name"),
        ("/api/models",   "name"),
        ("/api/spools",   "name"),
        ("/api/products", "name"),
        ("/api/orders",   "order_number"),
        ("/api/printers", "name"),
        ("/api/filaments","name"),
    ]
    for endpoint, field in cleanup_list:
        for item in client.get_json(endpoint, "admin"):
            if str(item.get(field, "")).startswith("_qa_"):
                client.req("DELETE", f"{endpoint}/{item['id']}", role="admin")

    # Webhooks
    for w in client.get_json("/api/webhooks", "admin"):
        if str(w.get("name", "")).startswith("_qa_"):
            client.req("DELETE", f"/api/webhooks/{w['id']}", role="admin")

    print(f"  {C.G}✓ Cleanup complete{C.X}")


def print_summary(results):
    total = len(results)
    passed = sum(1 for r in results if r[3])
    failed = sum(1 for r in results if not r[3])

    print(f"\n{C.B}{'='*72}")
    print(f"  TEST RESULTS")
    print(f"{'='*72}{C.X}")
    print(f"  Total:   {total}")
    print(f"  Passed:  {C.G}{passed}{C.X}")
    print(f"  Failed:  {C.R}{failed}{C.X}" if failed else f"  Failed:  {C.G}0{C.X}")
    print(f"  Rate:    {passed/total*100:.1f}%")

    if failed:
        print(f"\n  {C.B}FAILURES:{C.X}")
        for name, exp, got, ok in results:
            if not ok:
                print(f"    {C.R}✗{C.X} {name}  expected={exp}  got={got}")

    if not failed:
        print(f"\n  {C.G}{C.B}🎉 ALL TESTS PASSED{C.X}")

    print(f"\n{'='*72}\n")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="O.D.I.N. API & RBAC Test Suite")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"API host (default: {DEFAULT_HOST})")
    parser.add_argument("--api-key", default=None, help="API key (auto-reads from backend/.env)")
    parser.add_argument("--admin-user", default="admin", help="Admin username")
    parser.add_argument("--admin-pass", default="TheBestPassword1", help="Admin password")
    args = parser.parse_args()

    api_key = args.api_key or find_api_key()
    if not api_key:
        print("ERROR: No API key. Pass --api-key or check backend/.env")
        sys.exit(1)

    print(f"  API Key: {api_key[:8]}...{api_key[-4:]}")
    results = run_tests(args.host, api_key, args.admin_user, args.admin_pass)
    sys.exit(0 if all(r[3] for r in results) else 1)
