#!/usr/bin/env python3
"""
O.D.I.N. License Gating — Deep Patch

Gates Pro features inside:
1. Settings.jsx — hide Pro-only tabs (SSO, Webhooks, Email, Users, Permissions, Branding)
2. Printers.jsx — block Add Printer when at license limit
3. Admin.jsx — block Add User when at license limit

Run: python3 patch_deep_gating.py
Then: cd /opt/printfarm-scheduler/frontend && npm run build
"""

import os

steps = 0
errors = 0

def patch(filepath, old, new, label):
    global steps, errors
    try:
        content = open(filepath).read()
        if old not in content:
            if new in content:
                print(f"  ⚠️  {label} — already applied")
                return
            print(f"  ❌ {label} — target not found")
            print(f"      Looking for: {repr(old[:80])}...")
            errors += 1
            return
        content = content.replace(old, new, 1)
        open(filepath, 'w').write(content)
        steps += 1
        print(f"  ✅ {label}")
    except FileNotFoundError:
        print(f"  ❌ {label} — file not found: {filepath}")
        errors += 1

SETTINGS = '/opt/printfarm-scheduler/frontend/src/pages/Settings.jsx'
PRINTERS = '/opt/printfarm-scheduler/frontend/src/pages/Printers.jsx'
ADMIN = '/opt/printfarm-scheduler/frontend/src/pages/Admin.jsx'

print("=" * 60)
print("  O.D.I.N. Deep License Gating Patch")
print("=" * 60)
print()

# ──────────────────────────────────────────────
# 1. Settings.jsx — Filter TABS by license tier
# ──────────────────────────────────────────────
print("[1/3] Patching Settings.jsx...")

# 1a. Add useLicense import
patch(SETTINGS,
    "import { alertPreferences, smtpConfig } from '../api'",
    "import { alertPreferences, smtpConfig } from '../api'\nimport { useLicense } from '../LicenseContext'",
    "Add useLicense import"
)

# 1b. Add useLicense() hook call — insert after the activeTab state
patch(SETTINGS,
    "  const [activeTab, setActiveTab] = useState('general')",
    "  const [activeTab, setActiveTab] = useState('general')\n  const lic = useLicense()",
    "Add useLicense() hook"
)

# 1c. Replace static TABS with filtered version
# Pro-only tab IDs: sso, webhooks, email, users, permissions, branding
patch(SETTINGS,
    """  const TABS = [
    { id: 'general', label: 'General', icon: SettingsIcon },
    { id: 'alerts', label: 'Alerts', icon: Bell },
    { id: 'email', label: 'Email', icon: Mail },
    { id: 'sso', label: 'SSO', icon: Key },
    { id: 'webhooks', label: 'Webhooks', icon: Webhook },
    { id: 'users', label: 'Users', icon: Users },
    { id: 'permissions', label: 'Permissions', icon: Shield },
    { id: 'branding', label: 'Branding', icon: Palette },
    { id: 'data', label: 'Data', icon: Database },
  ]""",
    """  const PRO_TABS = ['sso', 'webhooks', 'email', 'users', 'permissions', 'branding']
  const ALL_TABS = [
    { id: 'general', label: 'General', icon: SettingsIcon },
    { id: 'alerts', label: 'Alerts', icon: Bell },
    { id: 'email', label: 'Email', icon: Mail },
    { id: 'sso', label: 'SSO', icon: Key },
    { id: 'webhooks', label: 'Webhooks', icon: Webhook },
    { id: 'users', label: 'Users', icon: Users },
    { id: 'permissions', label: 'Permissions', icon: Shield },
    { id: 'branding', label: 'Branding', icon: Palette },
    { id: 'data', label: 'Data', icon: Database },
  ]
  const TABS = lic.isPro ? ALL_TABS : ALL_TABS.filter(t => !PRO_TABS.includes(t.id))""",
    "Filter TABS by license tier"
)

print()

# ──────────────────────────────────────────────
# 2. Printers.jsx — Gate Add Printer at limit
# ──────────────────────────────────────────────
print("[2/3] Patching Printers.jsx...")

# 2a. Add useLicense import — find existing imports
patch(PRINTERS,
    "import { canDo } from '../permissions'",
    "import { canDo } from '../permissions'\nimport { useLicense } from '../LicenseContext'",
    "Add useLicense import to Printers"
)

# If the import line is different, try alternative
content = open(PRINTERS).read()
if "import { useLicense }" not in content:
    # Try finding the permissions import differently
    patch(PRINTERS,
        "import { canAccessPage, canDo } from '../permissions'",
        "import { canAccessPage, canDo } from '../permissions'\nimport { useLicense } from '../LicenseContext'",
        "Add useLicense import to Printers (alt)"
    )

# 2b. Add hook call — find the useQuery line for printers
patch(PRINTERS,
    "  const { data: printersData, isLoading } = useQuery({ queryKey: ['printers'], queryFn: () => printers.list() })",
    "  const { data: printersData, isLoading } = useQuery({ queryKey: ['printers'], queryFn: () => printers.list() })\n  const lic = useLicense()\n  const atLimit = lic.atPrinterLimit(printersData?.length || 0)",
    "Add useLicense() hook + atLimit check"
)

# 2c. Gate the Add Printer button (main)
patch(PRINTERS,
    """{canDo('printers.add') && <button onClick={() => setShowModal(true)} className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm self-start">
          <Plus size={16} /> Add Printer
        </button>}""",
    """{canDo('printers.add') && (atLimit
          ? <span className="flex items-center gap-2 px-4 py-2 bg-farm-700 text-farm-400 rounded-lg text-sm self-start cursor-not-allowed" title={`Printer limit reached (${lic.max_printers}). Upgrade to Pro for unlimited.`}>
              <Plus size={16} /> Add Printer (limit: {lic.max_printers})
            </span>
          : <button onClick={() => setShowModal(true)} className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm self-start">
              <Plus size={16} /> Add Printer
            </button>
        )}""",
    "Gate Add Printer button (main)"
)

# 2d. Gate the empty-state Add Printer button
patch(PRINTERS,
    """{canDo('printers.add') && <button onClick={() => setShowModal(true)} className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm">Add Your First Printer</button>}""",
    """{canDo('printers.add') && !atLimit && <button onClick={() => setShowModal(true)} className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm">Add Your First Printer</button>}""",
    "Gate Add Printer button (empty state)"
)

print()

# ──────────────────────────────────────────────
# 3. Admin.jsx — Gate Add User at limit
# ──────────────────────────────────────────────
print("[3/3] Patching Admin.jsx...")

# 3a. Add useLicense import
patch(ADMIN,
    "import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'",
    "import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'\nimport { useLicense } from '../LicenseContext'",
    "Add useLicense import to Admin"
)

# 3b. Add hook — after the useQuery for users
patch(ADMIN,
    "  const { data: users, isLoading } = useQuery({ queryKey: ['users'], queryFn: fetchUsers })",
    "  const { data: users, isLoading } = useQuery({ queryKey: ['users'], queryFn: fetchUsers })\n  const lic = useLicense()\n  const atUserLimit = lic.atUserLimit(users?.length || 0)",
    "Add useLicense() hook + atUserLimit check"
)

# 3c. Gate the Add User button
patch(ADMIN,
    """<button onClick={() => { setEditingUser(null); setShowModal(true) }} className="flex items-center gap-2 bg-print-600 hover:bg-print-500 px-4 py-2 rounded-lg font-medium transition-colors text-sm">
          <Plus size={16} /> Add User
        </button>""",
    """{atUserLimit
          ? <span className="flex items-center gap-2 bg-farm-700 text-farm-400 px-4 py-2 rounded-lg font-medium text-sm cursor-not-allowed" title={`User limit reached (${lic.max_users}). Upgrade to Pro for unlimited.`}>
              <Plus size={16} /> Add User (limit: {lic.max_users})
            </span>
          : <button onClick={() => { setEditingUser(null); setShowModal(true) }} className="flex items-center gap-2 bg-print-600 hover:bg-print-500 px-4 py-2 rounded-lg font-medium transition-colors text-sm">
              <Plus size={16} /> Add User
            </button>
        }""",
    "Gate Add User button"
)

print()
print("=" * 60)
print(f"  Done! {steps} patches applied, {errors} errors")
if errors > 0:
    print(f"  ⚠️  {errors} patches failed — check output above")
print()
print("  Build: cd /opt/printfarm-scheduler/frontend && npm run build")
print("  Restart: systemctl restart printfarm-backend")
print("=" * 60)
