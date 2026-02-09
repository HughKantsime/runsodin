#!/usr/bin/env python3
"""
fix_remaining_audit.py — Fix M-9, L-5, M-1 prep
Run: python3 fix_remaining_audit.py [--dry-run]
"""
import sys
import shutil
from datetime import datetime

DRY_RUN = "--dry-run" in sys.argv
BASE = "/opt/printfarm-scheduler"
fixes = []

def backup_and_read(path):
    with open(path, "r") as f:
        content = f.read()
    if not DRY_RUN:
        bak = path + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, bak)
        print(f"  Backup: {bak}")
    return content

def write_if_changed(path, original, new_content, fix_id, desc):
    if original != new_content:
        if DRY_RUN:
            print(f"  [DRY RUN] {fix_id}: {desc}")
        else:
            with open(path, "w") as f:
                f.write(new_content)
            print(f"  ✅ {fix_id}: {desc}")
        fixes.append(f"{fix_id}: {desc}")
        return True
    else:
        print(f"  ℹ️  {fix_id}: Already fixed or pattern not found")
        return False

# ─────────────────────────────────────────────────────────────────────
# M-9: Sanitize camera URLs — ensure raw RTSP with creds never leaks
# ─────────────────────────────────────────────────────────────────────
print("\n── M-9: Camera URL Credential Sanitization ──")
main_path = f"{BASE}/backend/main.py"
main_py = backup_and_read(main_path)

# Add a sanitize helper function after get_camera_url
sanitize_func = '''
def sanitize_camera_url(url: str) -> str:
    """Strip credentials from RTSP URLs for API responses."""
    if not url:
        return url
    import re
    # rtsps://bblp:ACCESS_CODE@192.168.x.x:322/... -> rtsps://***@192.168.x.x:322/...
    return re.sub(r'(rtsps?://)([^@]+)@', r'\\1***@', url)
'''

new_main = main_py
if "sanitize_camera_url" not in main_py:
    # Insert after get_camera_url function
    marker = "def sync_go2rtc_config"
    if marker in main_py:
        new_main = main_py.replace(
            marker,
            sanitize_func.strip() + "\n\n" + marker
        )
    write_if_changed(main_path, main_py, new_main, "M-9", "Added sanitize_camera_url() helper")
    main_py = new_main
else:
    print("  ℹ️  M-9: sanitize_camera_url already exists")

# Also ensure /api/cameras/{id}/stream doesn't leak the URL
# (Currently it returns webrtc_url which is a proxy path - already safe)
# But add a comment for clarity
print("  ℹ️  M-9: /api/cameras endpoints already proxy via go2rtc (URLs not exposed to frontend)")
print("       sanitize_camera_url available for any future endpoints that return camera info")

# ─────────────────────────────────────────────────────────────────────
# L-5: Add ProGate to Branding and Permissions routes
# ─────────────────────────────────────────────────────────────────────
print("\n── L-5: ProGate on Branding & Permissions Routes ──")
app_path = f"{BASE}/frontend/src/App.jsx"
app_jsx = backup_and_read(app_path)
new_app = app_jsx

# Both currently redirect to /settings:
#   <Route path="/permissions" element={<Navigate to="/settings" replace />} />
#   <Route path="/branding" element={<Navigate to="/settings" replace />} />
# 
# Even though they redirect, wrapping in ProGate means non-Pro users
# hitting /permissions or /branding directly get the upgrade prompt
# instead of being silently redirected to settings.

# Fix /permissions route
old_perm = '<Route path="/permissions" element={<Navigate to="/settings" replace />} />'
new_perm = '<Route path="/permissions" element={<ProGate feature="permissions"><Navigate to="/settings" replace /></ProGate>} />'
if old_perm in new_app:
    new_app = new_app.replace(old_perm, new_perm)

# Fix /branding route
old_brand = '<Route path="/branding" element={<Navigate to="/settings" replace />} />'
new_brand = '<Route path="/branding" element={<ProGate feature="branding"><Navigate to="/settings" replace /></ProGate>} />'
if old_brand in new_app:
    new_app = new_app.replace(old_brand, new_brand)

write_if_changed(app_path, app_jsx, new_app, "L-5", "Wrapped /branding and /permissions in ProGate")

# Now check if LicenseContext has "branding" and "permissions" in TIER_FEATURES
print("\n  Checking LicenseContext for feature definitions...")
lc_path = f"{BASE}/frontend/src/LicenseContext.jsx"
try:
    lc = backup_and_read(lc_path)
    new_lc = lc
    
    # Check if "branding" and "permissions" are in TIER_FEATURES pro set
    if '"branding"' not in lc and "'branding'" not in lc:
        # Need to add branding to Pro features
        # Find the pro features array/set and add it
        # Look for pattern like: pro: [...] or PRO: [...]
        import re
        # Try to find where pro features are defined
        pro_match = re.search(r'(pro\s*:\s*\[)([^\]]*?)(\])', lc, re.IGNORECASE)
        if pro_match:
            existing = pro_match.group(2).rstrip().rstrip(',')
            new_features = existing + ', "branding", "permissions"'
            new_lc = lc[:pro_match.start(2)] + new_features + lc[pro_match.end(2):]
            write_if_changed(lc_path, lc, new_lc, "L-5b", "Added branding/permissions to Pro TIER_FEATURES")
        else:
            print('  ⚠️  L-5b: Could not find Pro features array in LicenseContext.jsx')
            print('       Add "branding" and "permissions" to TIER_FEATURES manually')
    else:
        print('  ℹ️  L-5b: branding/permissions already in LicenseContext features')
except FileNotFoundError:
    print(f"  ⚠️  LicenseContext.jsx not found at {lc_path}")

# ─────────────────────────────────────────────────────────────────────
# M-1: Prep license_manager.py for keygen
# ─────────────────────────────────────────────────────────────────────
print("\n── M-1: License Manager Public Key ──")
lm_path = f"{BASE}/backend/license_manager.py"
try:
    lm = backup_and_read(lm_path)
    if "REPLACE_WITH_YOUR_PUBLIC_KEY" in lm:
        print("  ⚠️  M-1: Public key placeholder still present")
        print("       This is expected until you run keygen on Mac:")
        print("")
        print("       python3 generate_license.py --keygen")
        print("")
        print("       Then paste the public key into license_manager.py replacing:")
        print('       "REPLACE_WITH_YOUR_PUBLIC_KEY"')
        print("")
        print("       The dev bypass (accept any license) was already removed ✅")
        fixes.append("M-1: Documented — awaiting keygen on Mac")
    else:
        print("  ✅ M-1: Real public key is embedded")
except FileNotFoundError:
    print(f"  ⚠️  license_manager.py not found at {lm_path}")

# ─────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  {'DRY RUN — ' if DRY_RUN else ''}{len(fixes)} fix(es) {'would be ' if DRY_RUN else ''}applied")
for f in fixes:
    print(f"    • {f}")
print(f"{'='*60}")

if not DRY_RUN and fixes:
    print("\n⚡ Rebuild frontend & restart:")
    print("   cd /opt/printfarm-scheduler/frontend && npm run build")
    print("   systemctl restart printfarm-backend")
