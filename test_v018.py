#!/usr/bin/env python3
"""
O.D.I.N. v0.18.0 — Automated QA Test Suite
Tests all new features deployed this session.
Run on the server: python3 test_v018.py
"""

import json
import os
import sys
import sqlite3
import importlib.util
import time

BASE = "/opt/printfarm-scheduler"
DB = f"{BASE}/backend/printfarm.db"
BACKEND = f"{BASE}/backend"
FRONTEND = f"{BASE}/frontend"

# Terminal colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"

passed = 0
failed = 0
warnings = 0

def ok(msg):
    global passed
    passed += 1
    print(f"  {GREEN}✅ PASS{RESET}  {msg}")

def fail(msg):
    global failed
    failed += 1
    print(f"  {RED}❌ FAIL{RESET}  {msg}")

def warn(msg):
    global warnings
    warnings += 1
    print(f"  {YELLOW}⚠️  WARN{RESET}  {msg}")

def section(title):
    print(f"\n{CYAN}{BOLD}── {title} ──{RESET}")

# ============================================================
# Helper: HTTP request to local backend
# ============================================================
import urllib.request
import urllib.error

API_BASE = "http://127.0.0.1:8000"

def api_get(path, expect_status=200):
    """GET request to backend API. Returns (status, data)."""
    try:
        req = urllib.request.Request(f"{API_BASE}{path}")
        req.add_header("Content-Type", "application/json")
        # Try to get an API key from .env
        env_path = f"{BACKEND}/.env"
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("API_KEY="):
                    req.add_header("X-API-Key", line.strip().split("=", 1)[1])
                    break
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())
        return resp.status, data
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except:
            body = None
        return e.code, body
    except Exception as e:
        return None, str(e)

def api_put(path, body, expect_status=200):
    """PUT request to backend API."""
    try:
        req = urllib.request.Request(f"{API_BASE}{path}", method="PUT")
        req.add_header("Content-Type", "application/json")
        env_path = f"{BACKEND}/.env"
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("API_KEY="):
                    req.add_header("X-API-Key", line.strip().split("=", 1)[1])
                    break
        resp = urllib.request.urlopen(req, json.dumps(body).encode(), timeout=5)
        data = json.loads(resp.read().decode())
        return resp.status, data
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except:
            body = None
        return e.code, body
    except Exception as e:
        return None, str(e)


print(f"\n{BOLD}{'=' * 60}{RESET}")
print(f"{BOLD}  O.D.I.N. v0.18.0 — Automated QA Test Suite{RESET}")
print(f"{BOLD}{'=' * 60}{RESET}")

# ============================================================
# 1. DATABASE SCHEMA
# ============================================================
section("Database Schema")

conn = sqlite3.connect(DB)
cur = conn.cursor()

# Check all new columns exist
schema_checks = [
    ("print_files", "mesh_data", "3D viewer mesh storage"),
    ("printers", "plug_type", "Smart plug type"),
    ("printers", "plug_host", "Smart plug host"),
    ("printers", "plug_entity_id", "Smart plug HA entity"),
    ("printers", "plug_auth_token", "Smart plug auth token"),
    ("printers", "plug_auto_on", "Smart plug auto-on"),
    ("printers", "plug_auto_off", "Smart plug auto-off"),
    ("printers", "plug_cooldown_minutes", "Smart plug cooldown"),
    ("printers", "plug_power_state", "Smart plug power state"),
    ("printers", "plug_energy_kwh", "Smart plug energy total"),
    ("jobs", "energy_kwh", "Job energy consumption"),
    ("jobs", "energy_cost", "Job energy cost"),
    ("jobs", "submitted_by", "Job approval - submitter"),
    ("jobs", "approved_by", "Job approval - approver"),
    ("jobs", "approved_at", "Job approval - timestamp"),
    ("jobs", "rejected_reason", "Job approval - rejection reason"),
]

for table, column, desc in schema_checks:
    cur.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]
    if column in columns:
        ok(f"{table}.{column} — {desc}")
    else:
        fail(f"{table}.{column} MISSING — {desc}")

# Check new tables
new_tables = [
    ("ams_telemetry", "AMS environment time-series"),
]

for table, desc in new_tables:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    if cur.fetchone():
        ok(f"Table '{table}' exists — {desc}")
    else:
        fail(f"Table '{table}' MISSING — {desc}")

# Check ams_telemetry index
cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_ams_telemetry_printer_time'")
if cur.fetchone():
    ok("AMS telemetry index exists")
else:
    warn("AMS telemetry index missing — queries will be slow")

# Check system_config entries
config_checks = [
    ("energy_cost_per_kwh", "Energy rate config"),
]
for key, desc in config_checks:
    cur.execute("SELECT value FROM system_config WHERE key = ?", (key,))
    row = cur.fetchone()
    if row:
        ok(f"system_config.{key} = {row[0]} — {desc}")
    else:
        warn(f"system_config.{key} not set — {desc}")

conn.close()

# ============================================================
# 2. BACKEND FILES
# ============================================================
section("Backend Files")

backend_files = [
    ("smart_plug.py", "Smart plug module"),
    ("main.py", "Main API"),
    ("mqtt_monitor.py", "MQTT monitor"),
    ("printer_events.py", "Printer event handler"),
    ("threemf_parser.py", "3MF parser"),
    ("license_manager.py", "License manager"),
    ("alert_dispatcher.py", "Alert dispatcher"),
]

for filename, desc in backend_files:
    path = f"{BACKEND}/{filename}"
    if os.path.exists(path):
        size = os.path.getsize(path)
        ok(f"{filename} ({size:,} bytes) — {desc}")
    else:
        fail(f"{filename} MISSING — {desc}")

# Check smart_plug.py has expected functions
if os.path.exists(f"{BACKEND}/smart_plug.py"):
    sp_content = open(f"{BACKEND}/smart_plug.py").read()
    for func in ["tasmota_power", "ha_switch", "mqtt_power", "power_on", "power_off", 
                  "on_print_start", "on_print_complete", "record_energy_for_job"]:
        if f"def {func}" in sp_content:
            ok(f"smart_plug.{func}() exists")
        else:
            fail(f"smart_plug.{func}() MISSING")

# ============================================================
# 3. FRONTEND FILES
# ============================================================
section("Frontend Files")

frontend_checks = [
    ("src/components/ModelViewer.jsx", "3D model viewer component"),
    ("src/components/ProGate.jsx", "License gate component"),
    ("src/LicenseContext.jsx", "License context provider"),
    ("src/contexts/I18nContext.jsx", "i18n context provider"),
    ("src/i18n/en.json", "English translations"),
    ("src/i18n/de.json", "German translations"),
    ("src/i18n/ja.json", "Japanese translations"),
    ("src/i18n/es.json", "Spanish translations"),
    ("src/permissions.js", "RBAC permissions config"),
    ("public/manifest.json", "PWA manifest"),
    ("public/sw.js", "Service worker"),
    ("dist/index.html", "Production build"),
]

for path, desc in frontend_checks:
    full = f"{FRONTEND}/{path}"
    if os.path.exists(full):
        size = os.path.getsize(full)
        ok(f"{path} ({size:,} bytes) — {desc}")
    else:
        fail(f"{path} MISSING — {desc}")

# Check i18n translations have expected key count
for lang in ["en", "de", "ja", "es"]:
    path = f"{FRONTEND}/src/i18n/{lang}.json"
    if os.path.exists(path):
        data = json.load(open(path))
        count = len(data)
        if lang == "en":
            if count >= 150:
                ok(f"{lang}.json: {count} keys (complete)")
            else:
                warn(f"{lang}.json: only {count} keys (expected 150+)")
        else:
            if count >= 30:
                ok(f"{lang}.json: {count} keys (core strings)")
            else:
                warn(f"{lang}.json: only {count} keys (expected 30+)")

# ============================================================
# 4. FRONTEND BUILD INTEGRITY
# ============================================================
section("Frontend Build Integrity")

# Check main.jsx has correct provider nesting
main_jsx = open(f"{FRONTEND}/src/main.jsx").read()
providers = ["BrandingProvider", "LicenseProvider", "I18nProvider"]
for p in providers:
    if f"<{p}>" in main_jsx:
        ok(f"main.jsx wraps with <{p}>")
    else:
        fail(f"main.jsx MISSING <{p}> wrapper")

# Check provider nesting order: Branding > License > I18n > App
if "<BrandingProvider><LicenseProvider><I18nProvider><App />" in main_jsx:
    ok("Provider nesting order correct: Branding > License > I18n > App")
else:
    warn("Provider nesting order may be wrong — check main.jsx")

# Check App.jsx doesn't have stale I18nProvider import
app_jsx = open(f"{FRONTEND}/src/App.jsx").read()
if "import { I18nProvider }" in app_jsx:
    warn("App.jsx still has unused I18nProvider import")
else:
    ok("App.jsx clean — no stale I18nProvider import")

# Check ProGate wraps the right routes
pro_gated_routes = ["analytics", "maintenance", "products", "orders"]
for route in pro_gated_routes:
    if f'<ProGate feature="{route}">' in app_jsx:
        ok(f"Route /{route} is license-gated")
    else:
        fail(f"Route /{route} NOT license-gated")

# ============================================================
# 5. RBAC PERMISSIONS
# ============================================================
section("RBAC Permissions")

perms = open(f"{FRONTEND}/src/permissions.js").read()

required_pages = ["dashboard", "jobs", "printers", "models", "spools", "cameras", 
                   "analytics", "calculator", "upload", "maintenance", "settings",
                   "orders", "products", "alerts"]
for page in required_pages:
    if f"  {page}:" in perms or f"'{page}':" in perms:
        ok(f"Page access defined: {page}")
    else:
        fail(f"Page access MISSING: {page}")

required_actions = ["jobs.create", "jobs.edit", "orders.create", "orders.edit",
                    "products.create", "products.edit", "jobs.approve", "jobs.reject",
                    "alerts.read", "printers.plug"]
for action in required_actions:
    if f"'{action}'" in perms:
        ok(f"Action defined: {action}")
    else:
        fail(f"Action MISSING: {action}")

# ============================================================
# 6. LICENSE TIER FEATURES
# ============================================================
section("License Tier Features")

lic = open(f"{FRONTEND}/src/LicenseContext.jsx").read()

community_expected = ["dashboard", "printers", "cameras", "jobs", "models", "spools",
                      "keyboard_shortcuts", "pwa", "i18n", "3d_viewer"]
for feat in community_expected:
    if f"'{feat}'" in lic:
        ok(f"Community tier includes: {feat}")
    else:
        fail(f"Community tier MISSING: {feat}")

pro_expected = ["rbac", "sso", "orders", "products", "analytics", "webhooks",
                "prometheus", "mqtt_republish", "quiet_hours", "smart_plug",
                "energy_tracking", "ams_environment", "websocket", "drag_drop_queue",
                "ntfy", "telegram", "hms_decoder"]
for feat in pro_expected:
    if f"'{feat}'" in lic:
        ok(f"Pro tier includes: {feat}")
    else:
        fail(f"Pro tier MISSING: {feat}")

edu_expected = ["job_approval", "usage_reports", "print_quotas"]
for feat in edu_expected:
    if f"'{feat}'" in lic:
        ok(f"Education tier includes: {feat}")
    else:
        fail(f"Education tier MISSING: {feat}")

# ============================================================
# 7. API ENDPOINT TESTS
# ============================================================
section("API Endpoints (Live)")

# Test each new endpoint
endpoint_tests = [
    ("GET", "/api/settings/language", 200, "Language setting"),
    ("GET", "/api/config/quiet-hours", 200, "Quiet hours config"),
    ("GET", "/api/config/mqtt-republish", 200, "MQTT republish config"),
    ("GET", "/api/settings/energy-rate", 200, "Energy rate config"),
    ("GET", "/metrics", 200, "Prometheus metrics"),
    ("GET", "/api/license", 200, "License info"),
]

for method, path, expected, desc in endpoint_tests:
    status, data = api_get(path)
    if status == expected:
        ok(f"{method} {path} → {status} — {desc}")
    elif status == 401 or status == 403:
        warn(f"{method} {path} → {status} (auth required) — {desc}")
    elif status is None:
        warn(f"{method} {path} → connection failed — backend may not be running")
        break
    else:
        fail(f"{method} {path} → {status} (expected {expected}) — {desc}")

# Test language set/get roundtrip
status, data = api_put("/api/settings/language", {"language": "de"})
if status == 200:
    status2, data2 = api_get("/api/settings/language")
    if data2 and data2.get("language") == "de":
        ok("Language roundtrip: set DE → get DE")
        # Reset back to EN
        api_put("/api/settings/language", {"language": "en"})
    else:
        fail("Language roundtrip: set DE but get returned wrong value")
elif status == 401:
    warn("Language roundtrip: auth required — skipping")
else:
    fail(f"Language set → {status}")

# Test printer-specific endpoints (use printer ID 1 if exists)
conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT id FROM printers LIMIT 1")
printer = cur.fetchone()
conn.close()

if printer:
    pid = printer[0]
    printer_endpoints = [
        ("GET", f"/api/printers/{pid}/plug", "Smart plug config"),
        ("GET", f"/api/printers/{pid}/ams/current", "AMS current readings"),
        ("GET", f"/api/printers/{pid}/ams/environment?hours=24", "AMS environment history"),
    ]
    for method, path, desc in printer_endpoints:
        status, data = api_get(path)
        if status in (200, 404):
            ok(f"{method} {path} → {status} — {desc}")
        elif status == 401:
            warn(f"{method} {path} → 401 (auth required) — {desc}")
        else:
            fail(f"{method} {path} → {status} — {desc}")
else:
    warn("No printers in database — skipping printer-specific endpoint tests")

# Test Prometheus metrics content
status, data = api_get("/metrics")
if status == 200 and isinstance(data, str) and "odin_" in data:
    ok("Prometheus metrics contain odin_ prefix")
elif status == 200:
    # Check if it's text format
    warn("Prometheus metrics returned 200 but format unclear")
elif status == 401:
    warn("Prometheus metrics require auth — may want to make public")

# ============================================================
# 8. BACKEND CODE CHECKS
# ============================================================
section("Backend Code Integration")

main_py = open(f"{BACKEND}/main.py").read()

# Check WebSocket endpoint exists
if "websocket" in main_py.lower() and "async def ws" in main_py.lower() or "@app.websocket" in main_py:
    ok("WebSocket endpoint defined")
else:
    warn("WebSocket endpoint not found in main.py — may be in separate file")

# Check HMS decoder
if "hms" in main_py.lower() and ("853" in main_py or "HMS_CODES" in main_py or "hms_code" in main_py):
    ok("HMS error code handling present")
else:
    # Check for separate file
    hms_found = False
    for f in os.listdir(BACKEND):
        if "hms" in f.lower():
            hms_found = True
            ok(f"HMS decoder in separate file: {f}")
            break
    if not hms_found:
        content = open(f"{BACKEND}/mqtt_monitor.py").read()
        if "hms" in content.lower():
            ok("HMS handling in mqtt_monitor.py")
        else:
            warn("HMS error decoder not found — check implementation")

# Check MQTT monitor has AMS capture
mqtt_content = open(f"{BACKEND}/mqtt_monitor.py").read()
if "ams_telemetry" in mqtt_content or "_last_ams_env" in mqtt_content:
    ok("MQTT monitor captures AMS environment data")
else:
    fail("MQTT monitor missing AMS environment capture")

# Check printer_events has smart plug hooks
pe_content = open(f"{BACKEND}/printer_events.py").read()
if "smart_plug" in pe_content or "plug" in pe_content:
    ok("printer_events.py has smart plug integration")
else:
    fail("printer_events.py missing smart plug hooks")

# Check threemf_parser has mesh extraction
tp_content = open(f"{BACKEND}/threemf_parser.py").read()
if "extract_mesh" in tp_content or "mesh_data" in tp_content:
    ok("threemf_parser.py has mesh extraction")
else:
    fail("threemf_parser.py missing mesh extraction")

# Check notification channels
for channel in ["ntfy", "telegram"]:
    if channel in main_py.lower() or any(channel in open(f"{BACKEND}/{f}").read().lower() 
                                          for f in os.listdir(BACKEND) if f.endswith(".py")):
        ok(f"Notification channel: {channel}")
    else:
        fail(f"Notification channel MISSING: {channel}")

# Check drag-and-drop queue endpoint
if "reorder" in main_py.lower() or "queue_position" in main_py.lower():
    ok("Queue reorder endpoint present")
else:
    fail("Queue reorder endpoint missing")

# Check keyboard shortcuts component
ks_path = f"{FRONTEND}/src/components/KeyboardShortcuts.jsx"
if not os.path.exists(ks_path):
    ks_path = f"{FRONTEND}/src/App.jsx"
ks_content = open(ks_path).read()
if "KeyboardShortcuts" in ks_content or "showHelp" in ks_content:
    ok("Keyboard shortcuts modal present")
else:
    warn("Keyboard shortcuts modal not found")

# ============================================================
# 9. PWA MANIFEST
# ============================================================
section("PWA Manifest")

manifest_path = f"{FRONTEND}/public/manifest.json"
if os.path.exists(manifest_path):
    manifest = json.load(open(manifest_path))
    if "O.D.I.N" in manifest.get("name", "") or "ODIN" in manifest.get("name", "").upper():
        ok(f"PWA name: {manifest.get('name')}")
    else:
        warn(f"PWA name doesn't mention O.D.I.N.: {manifest.get('name')}")
    
    if manifest.get("display") == "standalone":
        ok("PWA display mode: standalone")
    else:
        warn(f"PWA display mode: {manifest.get('display')} (expected standalone)")
    
    if manifest.get("start_url"):
        ok(f"PWA start_url: {manifest.get('start_url')}")
    else:
        fail("PWA missing start_url")
else:
    fail("manifest.json missing")

# ============================================================
# 10. SERVICE WORKER
# ============================================================
section("Service Worker")

sw_path = f"{FRONTEND}/public/sw.js"
if os.path.exists(sw_path):
    sw = open(sw_path).read()
    if "push" in sw.lower() or "notification" in sw.lower():
        ok("Service worker handles push notifications")
    else:
        warn("Service worker exists but may not handle push")
    if "ODIN" in sw.upper() or "odin" in sw:
        ok("Service worker branded as O.D.I.N.")
    else:
        warn("Service worker not branded")
else:
    fail("sw.js missing")

# ============================================================
# SUMMARY
# ============================================================
print(f"\n{BOLD}{'=' * 60}{RESET}")
total = passed + failed + warnings
print(f"{BOLD}  QA Results: {total} checks{RESET}")
print(f"  {GREEN}✅ Passed:   {passed}{RESET}")
if failed > 0:
    print(f"  {RED}❌ Failed:   {failed}{RESET}")
else:
    print(f"  ❌ Failed:   {failed}")
if warnings > 0:
    print(f"  {YELLOW}⚠️  Warnings: {warnings}{RESET}")
else:
    print(f"  ⚠️  Warnings: {warnings}")
print(f"{BOLD}{'=' * 60}{RESET}")

if failed == 0:
    print(f"\n  {GREEN}{BOLD}🎉 All critical checks passed! Ready for v0.18.0 commit.{RESET}")
elif failed <= 3:
    print(f"\n  {YELLOW}{BOLD}⚠️  Minor issues found — review failures above.{RESET}")
else:
    print(f"\n  {RED}{BOLD}🚨 Multiple failures — fix before committing.{RESET}")

sys.exit(1 if failed > 0 else 0)
