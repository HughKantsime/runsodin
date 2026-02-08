#!/usr/bin/env python3
"""
Frontend License Gating for O.D.I.N.
=====================================
Hides/disables Pro features on Community tier.

Creates (overwrites if exist):
  - LicenseContext.jsx — React context providing license info app-wide
  - components/ProBadge.jsx — Small "PRO" badge component
  - components/ProGate.jsx — Wrapper that shows upgrade prompt for gated features

Modifies:
  - App.jsx — Adds ProBadge to gated sidebar nav items
  - api.js — Ensures license API functions exist

Safe to re-run: all writes are idempotent.
"""

import os

BASE = "/opt/printfarm-scheduler"
FRONTEND = f"{BASE}/frontend/src"

# =============================================================================
# 1. LicenseContext.jsx
# =============================================================================

license_context = r'''import { createContext, useContext, useState, useEffect } from 'react'
import { fetchAPI } from './api'

const LicenseContext = createContext({
  tier: 'community',
  licensee: null,
  expires: null,
  loading: true,
  isPro: false,
  isEducation: false,
  isEnterprise: false,
  hasFeature: () => false,
  refresh: () => {},
})

const TIER_FEATURES = {
  community: new Set([
    'dashboard', 'printers', 'cameras', 'jobs', 'upload',
    'models', 'spools', 'timeline', 'calculator',
  ]),
  pro: new Set([
    'dashboard', 'printers', 'cameras', 'jobs', 'upload',
    'models', 'spools', 'timeline', 'calculator',
    'unlimited_printers', 'unlimited_users',
    'rbac', 'sso', 'white_label', 'branding',
    'orders', 'products', 'bom',
    'webhooks', 'email_notifications',
    'analytics', 'csv_export',
    'maintenance', 'care_counters',
    'prometheus', 'mqtt_republish',
    'quiet_hours', 'permissions',
  ]),
  education: new Set([
    'job_approval', 'usage_reports', 'print_quotas', 'class_sections',
  ]),
  enterprise: new Set([
    'opcua', 'mqtt_republish_enterprise', 'audit_export', 'sqlcipher',
    'custom_integrations',
  ]),
}

function getFeaturesForTier(tier) {
  const tiers = ['community', 'pro', 'education', 'enterprise']
  const idx = tiers.indexOf(tier)
  if (idx === -1) return TIER_FEATURES.community
  const features = new Set()
  for (let i = 0; i <= idx; i++) {
    const tf = TIER_FEATURES[tiers[i]]
    if (tf) tf.forEach(f => features.add(f))
  }
  return features
}

export const PRO_PAGES = ['orders', 'products', 'analytics', 'maintenance', 'permissions', 'branding']
export const PRO_SETTINGS_TABS = ['sso', 'webhooks', 'smtp']

export function LicenseProvider({ children }) {
  const [license, setLicense] = useState({
    tier: 'community', licensee: null, expires: null, loading: true,
  })

  const fetchLicense = async () => {
    try {
      const data = await fetchAPI('/license')
      setLicense({
        tier: data.tier || 'community',
        licensee: data.licensee || null,
        expires: data.expires || null,
        loading: false,
      })
    } catch {
      setLicense(prev => ({ ...prev, tier: 'community', loading: false }))
    }
  }

  useEffect(() => { fetchLicense() }, [])

  const isPro = ['pro', 'education', 'enterprise'].includes(license.tier)
  const isEducation = ['education', 'enterprise'].includes(license.tier)
  const isEnterprise = license.tier === 'enterprise'
  const hasFeature = (feature) => getFeaturesForTier(license.tier).has(feature)

  const value = {
    ...license, isPro, isEducation, isEnterprise, hasFeature,
    isProPage: (page) => PRO_PAGES.includes(page),
    isProSettingsTab: (tab) => PRO_SETTINGS_TABS.includes(tab),
    refresh: fetchLicense,
  }

  return <LicenseContext.Provider value={value}>{children}</LicenseContext.Provider>
}

export function useLicense() {
  return useContext(LicenseContext)
}

export default LicenseContext
'''

with open(f"{FRONTEND}/LicenseContext.jsx", "w") as f:
    f.write(license_context)
print("✅ Created LicenseContext.jsx")

# =============================================================================
# 2. ProBadge.jsx
# =============================================================================

pro_badge = r'''export default function ProBadge({ className = '' }) {
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider bg-amber-500/20 text-amber-400 border border-amber-500/30 rounded ml-auto ${className}`}>
      PRO
    </span>
  )
}
'''

with open(f"{FRONTEND}/components/ProBadge.jsx", "w") as f:
    f.write(pro_badge)
print("✅ Created ProBadge.jsx")

# =============================================================================
# 3. ProGate.jsx
# =============================================================================

pro_gate = r'''import { useLicense } from '../LicenseContext'
import { Lock, ExternalLink } from 'lucide-react'

export default function ProGate({ feature, children, inline = false, tier = 'Pro' }) {
  const { hasFeature, loading } = useLicense()

  if (loading) return null
  if (hasFeature(feature)) return children

  if (inline) {
    return (
      <div className="relative">
        <div className="opacity-30 pointer-events-none select-none blur-[1px]">{children}</div>
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="bg-farm-950/90 border border-amber-500/30 rounded-lg px-4 py-3 flex items-center gap-2 shadow-lg">
            <Lock size={14} className="text-amber-400" />
            <span className="text-sm text-farm-300">
              Requires <span className="text-amber-400 font-medium">{tier}</span> license
            </span>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] text-center px-4">
      <div className="w-16 h-16 rounded-full bg-amber-500/10 border border-amber-500/20 flex items-center justify-center mb-6">
        <Lock size={28} className="text-amber-400" />
      </div>
      <h2 className="text-xl font-semibold text-farm-100 mb-2">{tier} Feature</h2>
      <p className="text-farm-400 max-w-md mb-6">
        This feature requires an O.D.I.N. <span className="text-amber-400 font-medium">{tier}</span> license.
        Upgrade to unlock unlimited printers, multi-user RBAC, orders tracking, analytics, and more.
      </p>
      <a href="https://runsodin.com/#pricing" target="_blank" rel="noopener noreferrer"
        className="inline-flex items-center gap-2 px-5 py-2.5 bg-amber-500 hover:bg-amber-400 text-farm-950 font-semibold rounded transition-colors">
        View Pricing <ExternalLink size={14} />
      </a>
    </div>
  )
}
'''

with open(f"{FRONTEND}/components/ProGate.jsx", "w") as f:
    f.write(pro_gate)
print("✅ Created ProGate.jsx")

# =============================================================================
# 4. Patch App.jsx — add ProBadge to gated nav items
# =============================================================================

app_path = f"{FRONTEND}/App.jsx"
with open(app_path, "r") as f:
    app = f.read()

changes = 0

# Fix LicenseContext import path if pointing to contexts/
if "from './contexts/LicenseContext'" in app:
    app = app.replace("from './contexts/LicenseContext'", "from './LicenseContext'")
    changes += 1
    print("✅ Fixed LicenseContext import path")

# Ensure ProBadge import
if "import ProBadge" not in app:
    if "import ProGate" in app:
        app = app.replace(
            "import ProGate from './components/ProGate'",
            "import ProGate from './components/ProGate'\nimport ProBadge from './components/ProBadge'"
        )
    else:
        app = app.replace(
            "import AlertBell from './components/AlertBell'",
            "import ProGate from './components/ProGate'\nimport ProBadge from './components/ProBadge'\nimport AlertBell from './components/AlertBell'"
        )
    changes += 1
    print("✅ Added ProBadge import")

# Add ProBadge after gated nav item text
# Pattern: >Orders</NavItem>  →  >Orders{!lic.isPro && <ProBadge />}</NavItem>
gated_items = ['Orders', 'Products', 'Analytics', 'Maintenance']
for item in gated_items:
    target = f'>{item}</NavItem>'
    replacement = f'>{item}{{!lic.isPro && <ProBadge />}}</NavItem>'
    if target in app and replacement not in app:
        app = app.replace(target, replacement)
        changes += 1
        print(f"✅ Added ProBadge to {item} nav item")

# Wrap gated page routes with ProGate
# Pattern: element={<Orders />}  →  element={<ProGate feature="orders"><Orders /></ProGate>}
gated_routes = {
    'Orders': 'orders',
    'Products': 'products',
    'Analytics': 'analytics',
    'Maintenance': 'maintenance',
}
for component, feature in gated_routes.items():
    old = f'element={{<{component} />}}'
    new = f'element={{<ProGate feature="{feature}"><{component} /></ProGate>}}'
    if old in app and new not in app:
        app = app.replace(old, new)
        changes += 1
        print(f"✅ Wrapped {component} route with ProGate")

with open(app_path, "w") as f:
    f.write(app)
print(f"\nApp.jsx: {changes} total changes")

# =============================================================================
# 5. Ensure api.js has license functions
# =============================================================================

api_path = f"{FRONTEND}/api.js"
if os.path.exists(api_path):
    with open(api_path, "r") as f:
        api = f.read()

    if "license" not in api.lower() or "getLicense" not in api:
        license_api = """
// License
export const licenseApi = {
  get: () => fetchAPI('/license'),
  upload: (formData) => fetch('/api/license/upload', {
    method: 'POST',
    headers: { 'X-API-Key': localStorage.getItem('pf_api_key') || '' },
    body: formData,
  }).then(r => r.json()),
  remove: () => fetchAPI('/license', { method: 'DELETE' }),
}
"""
        api = api.rstrip() + "\n" + license_api + "\n"
        with open(api_path, "w") as f:
            f.write(api)
        print("✅ Added license API to api.js")
    else:
        print("· api.js already has license functions")

print("\n" + "=" * 60)
print("✅ Frontend license gating complete!")
print("=" * 60)
print("""
Deploy:
  scp ~/Downloads/add_license_gating.py root@192.168.70.200:/opt/printfarm-scheduler/
  ssh root@192.168.70.200

  cd /opt/printfarm-scheduler
  python3 add_license_gating.py
  cd frontend && npm run build
  systemctl restart printfarm-backend
""")
