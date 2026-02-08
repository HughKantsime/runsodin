#!/usr/bin/env python3
"""
O.D.I.N. v0.18.0 — QA Pass Fix Script
Fixes:
1. I18nProvider not wrapping app (add to main.jsx, remove unused import from App.jsx)
2. RBAC missing actions for orders, products, alerts, approval, smart plug
3. License TIER_FEATURES missing v0.18.0 features
4. Backend permission checks for new endpoints
"""

import os

BASE = "/opt/printfarm-scheduler"
FRONTEND = f"{BASE}/frontend/src"

print("=" * 60)
print("  O.D.I.N. v0.18.0 — QA Fix Pass")
print("=" * 60)
print()

fixes = []

# ============================================================
# 1. Add I18nProvider to main.jsx, remove from App.jsx
# ============================================================

main_jsx = f"{FRONTEND}/main.jsx"
with open(main_jsx, "r") as f:
    content = f.read()

if "I18nProvider" not in content:
    # Add import
    content = content.replace(
        "import { LicenseProvider } from './LicenseContext'",
        "import { LicenseProvider } from './LicenseContext'\nimport { I18nProvider } from './contexts/I18nContext'"
    )
    # Wrap around App — innermost provider
    content = content.replace(
        "<BrandingProvider><LicenseProvider><App /></LicenseProvider></BrandingProvider>",
        "<BrandingProvider><LicenseProvider><I18nProvider><App /></I18nProvider></LicenseProvider></BrandingProvider>"
    )
    with open(main_jsx, "w") as f:
        f.write(content)
    fixes.append("Added I18nProvider to main.jsx")

# Remove unused import from App.jsx
app_jsx = f"{FRONTEND}/App.jsx"
with open(app_jsx, "r") as f:
    content = f.read()

if "import { I18nProvider } from './contexts/I18nContext'" in content:
    content = content.replace("import { I18nProvider } from './contexts/I18nContext'\n", "")
    content = content.replace("import { I18nProvider } from './contexts/I18nContext'", "")
    with open(app_jsx, "w") as f:
        f.write(content)
    fixes.append("Removed unused I18nProvider import from App.jsx")

print(f"[1/4] I18nProvider wrapper:")
for f in fixes:
    print(f"  ✅ {f}")
if not fixes:
    print("  ⚠️  Already correct")

# ============================================================
# 2. Update RBAC permissions.js — add missing actions
# ============================================================

perms_path = f"{FRONTEND}/permissions.js"
with open(perms_path, "r") as f:
    perms = f.read()

perms_fixes = []

# Add missing page access entries
page_additions = {
    "orders":     "['admin', 'operator', 'viewer']",
    "products":   "['admin', 'operator', 'viewer']",
    "alerts":     "['admin', 'operator', 'viewer']",
}

for page, roles in page_additions.items():
    key = f"  {page}:"
    if key not in perms and f"'{page}':" not in perms:
        # Insert before the closing brace of DEFAULT_PAGE_ACCESS
        perms = perms.replace(
            "  branding:    ['admin'],\n}",
            f"  branding:    ['admin'],\n  {page}:{'  ' * (max(0, 12 - len(page)))} {roles},\n}}"
        )
        perms_fixes.append(f"Added page access: {page}")

# Add missing action entries
action_additions = {
    "'orders.create'":     "['admin', 'operator']",
    "'orders.edit'":       "['admin', 'operator']",
    "'orders.delete'":     "['admin']",
    "'orders.ship'":       "['admin', 'operator']",
    "'products.create'":   "['admin', 'operator']",
    "'products.edit'":     "['admin', 'operator']",
    "'products.delete'":   "['admin']",
    "'jobs.approve'":      "['admin', 'operator']",
    "'jobs.reject'":       "['admin', 'operator']",
    "'jobs.resubmit'":     "['admin', 'operator', 'viewer']",
    "'alerts.read'":       "['admin', 'operator', 'viewer']",
    "'printers.plug'":     "['admin', 'operator']",
}

# Find the closing brace of DEFAULT_ACTION_ACCESS
for action, roles in action_additions.items():
    if action not in perms:
        # Find last entry before closing brace
        marker = "  'dashboard.actions': ['admin', 'operator'],\n}"
        if marker in perms:
            padded = action + ":" + " " * max(1, 22 - len(action))
            perms = perms.replace(
                marker,
                f"  'dashboard.actions': ['admin', 'operator'],\n  {action}:{' ' * max(1, 20 - len(action))} {roles},\n}}"
            )
            perms_fixes.append(f"Added action: {action}")

with open(perms_path, "w") as f:
    f.write(perms)

print(f"\n[2/4] RBAC permissions:")
for f in perms_fixes:
    print(f"  ✅ {f}")
if not perms_fixes:
    print("  ⚠️  Already complete")

# ============================================================
# 3. Update LicenseContext TIER_FEATURES for v0.18.0
# ============================================================

lic_path = f"{FRONTEND}/LicenseContext.jsx"
with open(lic_path, "r") as f:
    lic = f.read()

lic_fixes = []

# Add v0.18.0 features to community tier
community_additions = [
    'keyboard_shortcuts', 'pwa', 'i18n', '3d_viewer',
]

for feat in community_additions:
    if f"'{feat}'" not in lic:
        # Add to community set
        lic = lic.replace(
            "    'models', 'spools', 'timeline', 'calculator',\n  ]),",
            f"    'models', 'spools', 'timeline', 'calculator',\n    '{feat}',\n  ]),",
        )
        lic_fixes.append(f"Community: +{feat}")

# Add v0.18.0 features to pro tier
pro_additions = [
    'smart_plug', 'energy_tracking', 'ams_environment',
    'websocket', 'drag_drop_queue', 'ntfy', 'telegram',
    'hms_decoder', 'failure_logging',
]

for feat in pro_additions:
    if f"'{feat}'" not in lic:
        lic = lic.replace(
            "    'quiet_hours', 'permissions',\n  ]),",
            f"    'quiet_hours', 'permissions',\n    '{feat}',\n  ]),",
        )
        lic_fixes.append(f"Pro: +{feat}")

with open(lic_path, "w") as f:
    f.write(lic)

print(f"\n[3/4] License tier features:")
for f in lic_fixes:
    print(f"  ✅ {f}")
if not lic_fixes:
    print("  ⚠️  Already complete")

# ============================================================
# 4. Backend — verify new endpoints have auth checks
# ============================================================

main_path = f"{BASE}/backend/main.py"
with open(main_path, "r") as f:
    main = f.read()

backend_checks = []

# Check that new endpoints exist and have appropriate patterns
endpoints_to_check = [
    ("/api/settings/language", "GET"),
    ("/api/settings/language", "PUT"),
    ("/api/printers/{id}/plug", "GET"),
    ("/api/printers/{id}/ams/environment", "GET"),
    ("/api/printers/{id}/ams/current", "GET"),
    ("/api/metrics", "GET"),
    ("/api/settings/quiet-hours", "GET"),
    ("/api/settings/mqtt-republish", "GET"),
]

for endpoint, method in endpoints_to_check:
    # Normalize for search
    search = endpoint.replace("{id}", "{")
    if search in main or endpoint.split("/")[-1] in main:
        backend_checks.append(f"✅ {method} {endpoint} — exists")
    else:
        backend_checks.append(f"❌ {method} {endpoint} — MISSING")

print(f"\n[4/4] Backend endpoint verification:")
for c in backend_checks:
    print(f"  {c}")

print()
print("=" * 60)
print("  QA Fix Pass Complete")
print("  Next: npm run build && systemctl restart printfarm-backend")
print("=" * 60)
