#!/usr/bin/env python3
"""
O.D.I.N. License Gating — Frontend Patch

Wires LicenseContext into the app:
1. main.jsx — adds LicenseProvider wrapper
2. App.jsx — imports useLicense, gates Pro pages in sidebar
3. App.jsx — wraps Pro-only routes with ProGate component

Prerequisites:
  - Copy LicenseContext.jsx to frontend/src/
  - Copy ProGate.jsx to frontend/src/components/

Run: python3 patch_license_gating.py
Then: cd /opt/printfarm-scheduler/frontend && npm run build
"""

import os
import sys

BASE = '/opt/printfarm-scheduler/frontend/src'
steps = 0
errors = 0

def patch(filepath, old, new, label):
    global steps, errors
    fullpath = os.path.join(BASE, filepath) if not filepath.startswith('/') else filepath
    try:
        content = open(fullpath).read()
        if old not in content:
            if new in content:
                print(f"  ⚠️  {label} — already applied")
                return
            print(f"  ❌ {label} — target string not found")
            print(f"      Looking for: {old[:80]}...")
            errors += 1
            return
        content = content.replace(old, new, 1)
        open(fullpath, 'w').write(content)
        steps += 1
        print(f"  ✅ {label}")
    except FileNotFoundError:
        print(f"  ❌ {label} — file not found: {fullpath}")
        errors += 1

print("=" * 60)
print("  O.D.I.N. License Gating — Frontend Patch")
print("=" * 60)
print()

# ──────────────────────────────────────────────
# 1. main.jsx — Add LicenseProvider
# ──────────────────────────────────────────────
print("[1/3] Patching main.jsx...")

patch('main.jsx',
    "import { BrandingProvider } from './BrandingContext'",
    "import { BrandingProvider } from './BrandingContext'\nimport { LicenseProvider } from './LicenseContext'",
    "Add LicenseProvider import"
)

patch('main.jsx',
    "<BrandingProvider><App /></BrandingProvider>",
    "<BrandingProvider><LicenseProvider><App /></LicenseProvider></BrandingProvider>",
    "Wrap App with LicenseProvider"
)

print()

# ──────────────────────────────────────────────
# 2. App.jsx — Import useLicense + ProGate
# ──────────────────────────────────────────────
print("[2/3] Patching App.jsx imports...")

patch('App.jsx',
    "import { useBranding } from './BrandingContext'",
    "import { useBranding } from './BrandingContext'\nimport { useLicense } from './LicenseContext'\nimport ProGate from './components/ProGate'",
    "Add useLicense + ProGate imports"
)

print()

# ──────────────────────────────────────────────
# 3. App.jsx — Add license hook + gate sidebar
# ──────────────────────────────────────────────
print("[3/3] Patching App.jsx sidebar gating...")

# 3a. Add useLicense() hook call near the adv variable
patch('App.jsx',
    "  const adv = uiMode === 'advanced'",
    "  const adv = uiMode === 'advanced'\n  const lic = useLicense()",
    "Add useLicense() hook"
)

# 3b. Gate Orders in sidebar — add lic.isPro check
# Current: {adv && canAccessPage('jobs') && <NavItem ... to="/orders" ...>Orders</NavItem>}
patch('App.jsx',
    '{adv && canAccessPage(\'jobs\') && <NavItem collapsed={collapsed && !mobileOpen} to="/orders" icon={ShoppingCart} onClick={handleNavClick}>Orders</NavItem>}',
    '{adv && lic.isPro && canAccessPage(\'jobs\') && <NavItem collapsed={collapsed && !mobileOpen} to="/orders" icon={ShoppingCart} onClick={handleNavClick}>Orders</NavItem>}',
    "Gate Orders nav (Pro)"
)

# 3c. Gate Products in sidebar
patch('App.jsx',
    '{adv && canAccessPage(\'models\') && <NavItem collapsed={collapsed && !mobileOpen} to="/products" icon={ShoppingBag} onClick={handleNavClick}>Products</NavItem>}',
    '{adv && lic.isPro && canAccessPage(\'models\') && <NavItem collapsed={collapsed && !mobileOpen} to="/products" icon={ShoppingBag} onClick={handleNavClick}>Products</NavItem>}',
    "Gate Products nav (Pro)"
)

# 3d. Gate Maintenance in sidebar
patch('App.jsx',
    "{canAccessPage('maintenance') && <NavItem collapsed={collapsed && !mobileOpen} to=\"/maintenance\" icon={Wrench} onClick={handleNavClick}>Maintenance</NavItem>}",
    "{lic.isPro && canAccessPage('maintenance') && <NavItem collapsed={collapsed && !mobileOpen} to=\"/maintenance\" icon={Wrench} onClick={handleNavClick}>Maintenance</NavItem>}",
    "Gate Maintenance nav (Pro)"
)

# 3e. Gate Analytics in sidebar
patch('App.jsx',
    "{canAccessPage('analytics') && <NavItem collapsed={collapsed && !mobileOpen} to=\"/analytics\" icon={BarChart3} onClick={handleNavClick}>Analytics</NavItem>}",
    "{lic.isPro && canAccessPage('analytics') && <NavItem collapsed={collapsed && !mobileOpen} to=\"/analytics\" icon={BarChart3} onClick={handleNavClick}>Analytics</NavItem>}",
    "Gate Analytics nav (Pro)"
)

# 3f. Gate the Monitor group header — show if Pro and adv
patch('App.jsx',
    '{adv && (canAccessPage("analytics") || canAccessPage("maintenance")) && <NavGroup label="Monitor"',
    '{adv && lic.isPro && (canAccessPage("analytics") || canAccessPage("maintenance")) && <NavGroup label="Monitor"',
    "Gate Monitor group header (Pro)"
)

# 3g. Gate Monitor group content
patch('App.jsx',
    '{adv && ((collapsed && !mobileOpen) || sections.monitor) && <>',
    '{adv && lic.isPro && ((collapsed && !mobileOpen) || sections.monitor) && <>',
    "Gate Monitor group content (Pro)"
)

print()
print("=" * 60)
print(f"  Done! {steps} patches applied, {errors} errors")
if errors > 0:
    print(f"  ⚠️  {errors} patches failed — check output above")
print()
print("  Next steps:")
print("  1. Review changes: grep 'lic.isPro' App.jsx")
print("  2. Build: cd /opt/printfarm-scheduler/frontend && npm run build")
print("  3. Restart: systemctl restart printfarm-backend")
print("  4. Test: load app with no license file → Pro nav items should be hidden")
print("=" * 60)
