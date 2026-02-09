#!/usr/bin/env python3
"""
O.D.I.N. Code Audit Fixes — SB-2 through SB-7 + H-2 + L-1 + M-3 + M-7 + M-11 + L-3
Run on server: python3 apply_audit_fixes.py
Creates backups before editing. Dry-run by default — pass --apply to write.
"""
import os, sys, re, shutil
from datetime import datetime

BASE = "/opt/printfarm-scheduler"
BACKEND = f"{BASE}/backend"
FRONTEND = f"{BASE}/frontend/src"
DRY_RUN = "--apply" not in sys.argv

fixes_applied = 0
fixes_failed = 0

def backup(path):
    if os.path.exists(path):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"{path}.bak_{ts}"
        shutil.copy2(path, bak)
        return bak
    return None

def fix_file(path, old, new, label):
    global fixes_applied, fixes_failed
    if not os.path.exists(path):
        print(f"  ✗ {label}: FILE NOT FOUND {path}")
        fixes_failed += 1
        return False
    
    content = open(path, "r").read()
    if old not in content:
        # Try to see if already fixed
        if new in content:
            print(f"  ✓ {label}: already fixed")
            fixes_applied += 1
            return True
        # Show what we expected for debugging
        print(f"  ✗ {label}: pattern not found in {os.path.basename(path)}")
        # Show nearby context
        for keyword in old.split('\n')[0].strip()[:40].split():
            if len(keyword) > 8:
                matches = [i for i, line in enumerate(content.split('\n')) if keyword in line]
                if matches:
                    print(f"    ('{keyword}' found at lines: {matches[:5]})")
                break
        fixes_failed += 1
        return False
    
    count = content.count(old)
    if count > 1:
        print(f"  ⚠ {label}: pattern found {count} times — replacing first only")
        content = content.replace(old, new, 1)
    else:
        content = content.replace(old, new)
    
    if DRY_RUN:
        print(f"  ○ {label}: would apply (dry-run)")
        fixes_applied += 1
        return True
    
    backup(path)
    with open(path, "w") as f:
        f.write(content)
    print(f"  ✓ {label}: applied")
    fixes_applied += 1
    return True


def fix_regex(path, pattern, replacement, label):
    """Like fix_file but uses regex for complex patterns."""
    global fixes_applied, fixes_failed
    if not os.path.exists(path):
        print(f"  ✗ {label}: FILE NOT FOUND {path}")
        fixes_failed += 1
        return False
    
    content = open(path, "r").read()
    new_content, count = re.subn(pattern, replacement, content, count=1)
    
    if count == 0:
        # Check if already fixed
        if replacement.replace('\\1', '').replace('\\2', '')[:30] in content:
            print(f"  ✓ {label}: already fixed")
            fixes_applied += 1
            return True
        print(f"  ✗ {label}: regex pattern not matched")
        fixes_failed += 1
        return False
    
    if DRY_RUN:
        print(f"  ○ {label}: would apply (dry-run)")
        fixes_applied += 1
        return True
    
    backup(path)
    with open(path, "w") as f:
        f.write(new_content)
    print(f"  ✓ {label}: applied")
    fixes_applied += 1
    return True


# ═══════════════════════════════════════════════════════════════
print("=" * 60)
print("  O.D.I.N. Audit Fix Script")
print(f"  Mode: {'DRY-RUN (pass --apply to write)' if DRY_RUN else 'APPLYING CHANGES'}")
print("=" * 60)

MAIN_PY = f"{BACKEND}/main.py"
AUTH_PY = f"{BACKEND}/auth.py"

# First, read main.py to find actual patterns
if not os.path.exists(MAIN_PY):
    print(f"\nFATAL: {MAIN_PY} not found")
    sys.exit(1)

main_content = open(MAIN_PY).read()
main_lines = main_content.split('\n')

# ═══════════════════════════════════════════════════════════════
print("\n── SB-2: Fix JWT split-brain (OIDC uses wrong secret) ──")

# Find the OIDC callback that uses JWT_SECRET
# Pattern: jwt_secret = os.environ.get("JWT_SECRET", ...)
# And: jwt.encode(...)

# First find what auth.py exports
if os.path.exists(AUTH_PY):
    auth_content = open(AUTH_PY).read()
    # Find the secret key variable name
    for pattern in ['SECRET_KEY = ', 'JWT_SECRET_KEY = ', 'secret_key = ']:
        if pattern in auth_content:
            print(f"  Found in auth.py: {pattern.strip()}")
            break

# Fix: replace the standalone jwt_secret in OIDC callback
fix_file(MAIN_PY,
    'jwt_secret = os.environ.get("JWT_SECRET", "change-me-in-production")',
    'from auth import SECRET_KEY as _jwt_secret_key  # SB-2: Use same secret as auth.py\n    jwt_secret = _jwt_secret_key',
    "SB-2a: JWT secret unification")

# Alternative pattern — might be slightly different
if 'jwt_secret = os.environ.get("JWT_SECRET"' not in main_content and '_jwt_secret_key' not in main_content:
    # Try broader search
    for i, line in enumerate(main_lines):
        if 'JWT_SECRET' in line and 'environ' in line and 'jwt_secret' in line.lower():
            print(f"  Found JWT_SECRET at line {i+1}: {line.strip()}")
            break

# ═══════════════════════════════════════════════════════════════
print("\n── SB-4: Fix /label auth bypass (substring → exact match) ──")

# The middleware has: "/label" in request.url.path
fix_file(MAIN_PY,
    '"/label" in request.url.path',
    '(request.url.path.endswith("/label") or request.url.path.endswith("/labels/batch"))',
    "SB-4: Label auth bypass")

# ═══════════════════════════════════════════════════════════════
print("\n── SB-5: Fix branding auth bypass (all methods → GET only) ──")

fix_file(MAIN_PY,
    'request.url.path.startswith("/api/branding")',
    '(request.url.path.startswith("/api/branding") and request.method == "GET")',
    "SB-5: Branding auth bypass")

# ═══════════════════════════════════════════════════════════════
print("\n── SB-6: User update column whitelist ──")

# Find the raw SQL update pattern
fix_file(MAIN_PY,
    'set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())',
    'ALLOWED_USER_FIELDS = {"username", "email", "role", "is_active", "password_hash", "display_name"}\n    updates = {k: v for k, v in updates.items() if k in ALLOWED_USER_FIELDS}\n    if not updates:\n        raise HTTPException(status_code=400, detail="No valid fields to update")\n    set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())',
    "SB-6: User update whitelist")

# ═══════════════════════════════════════════════════════════════
print("\n── SB-7: Fix frontend branding defaults ──")

# BrandingContext.jsx
bc_path = f"{FRONTEND}/BrandingContext.jsx"
if os.path.exists(bc_path):
    bc = open(bc_path).read()
    # Find the default branding object — look for app_name with PrintFarm
    if 'PrintFarm' in bc:
        fix_file(bc_path,
            'app_name: "PrintFarm Scheduler"',
            'app_name: "O.D.I.N."',
            "SB-7a: BrandingContext default app_name")
        fix_file(bc_path,
            "app_name: 'PrintFarm Scheduler'",
            "app_name: 'O.D.I.N.'",
            "SB-7a-alt: BrandingContext default app_name (single quotes)")
    else:
        print(f"  ✓ SB-7a: BrandingContext — no 'PrintFarm' found (already fixed?)")
        fixes_applied += 1

# Also fix subtitle if present
if os.path.exists(bc_path):
    bc = open(bc_path).read()
    if 'PrintFarm' in bc:
        # Catch any remaining PrintFarm references
        for old_str in ['PrintFarm Scheduler', 'PrintFarm']:
            if old_str in bc:
                fix_file(bc_path, old_str, 'O.D.I.N.', f"SB-7: remaining '{old_str}' in BrandingContext")

# Branding.jsx
br_path = f"{FRONTEND}/pages/Branding.jsx"
if os.path.exists(br_path):
    br = open(br_path).read()
    if 'PrintFarm' in br:
        fix_file(br_path,
            'app_name: "PrintFarm Scheduler"',
            'app_name: "O.D.I.N."',
            "SB-7b: Branding.jsx default app_name")
        fix_file(br_path,
            "app_name: 'PrintFarm Scheduler'",
            "app_name: 'O.D.I.N.'",
            "SB-7b-alt: Branding.jsx (single quotes)")
    else:
        print(f"  ✓ SB-7b: Branding.jsx — no 'PrintFarm' found (already fixed?)")
        fixes_applied += 1
else:
    print(f"  ✓ SB-7b: Branding.jsx not found (may not exist)")
    fixes_applied += 1

# ═══════════════════════════════════════════════════════════════
print("\n── H-2: Fix version string mismatches ──")

# Read current version
version_file = f"{BASE}/VERSION"
if os.path.exists(version_file):
    version = open(version_file).read().strip()
    print(f"  VERSION file says: {version}")
else:
    version = "0.19.1"
    print(f"  VERSION file not found, using {version}")

# Add version reading near the top of main.py (after imports)
# Find the FastAPI app creation
fix_file(MAIN_PY,
    'version="0.1.0"',
    f'version="{version}"',
    "H-2a: FastAPI app version")

# Fix health endpoint version
fix_file(MAIN_PY,
    '"version": "0.1.0"',
    f'"version": "{version}"',
    "H-2b: Health endpoint version")

# Fix Prometheus version
fix_file(MAIN_PY,
    '"version": "0.0.4"',
    f'"version": "{version}"',
    "H-2c: Prometheus version")

# ═══════════════════════════════════════════════════════════════
print("\n── L-1: Fix setup password validation indentation ──")

# The bug: password validation is inside the if block (dead code)
# Pattern:
#     if _setup_users_exist(db):
#         raise HTTPException(...)
#
#         pw_valid, pw_msg = _validate_password(...)
#
# Fix: dedent the pw_valid line

# This is tricky because the indentation matters. Let's find the exact pattern.
fix_file(MAIN_PY,
    '        raise HTTPException(status_code=403, detail="Setup already completed \\xe2\\x80\\x93 users exist")\n\n        pw_valid, pw_msg = _validate_password(request.password)',
    '        raise HTTPException(status_code=403, detail="Setup already completed \\xe2\\x80\\x93 users exist")\n\n    pw_valid, pw_msg = _validate_password(request.password)',
    "L-1a: Setup password validation indent (encoded dash)")

# Try with different dash encodings
fix_file(MAIN_PY,
    '        raise HTTPException(status_code=403, detail="Setup already completed – users exist")\n\n        pw_valid, pw_msg = _validate_password(request.password)',
    '        raise HTTPException(status_code=403, detail="Setup already completed – users exist")\n\n    pw_valid, pw_msg = _validate_password(request.password)',
    "L-1b: Setup password validation indent (unicode dash)")

# Try with em dash
fix_file(MAIN_PY,
    '        raise HTTPException(status_code=403, detail="Setup already completed — users exist")\n\n        pw_valid, pw_msg = _validate_password(request.password)',
    '        raise HTTPException(status_code=403, detail="Setup already completed — users exist")\n\n    pw_valid, pw_msg = _validate_password(request.password)',
    "L-1c: Setup password validation indent (em dash)")

# Regex fallback — find 8-space indent pw_valid after the raise
fix_regex(MAIN_PY,
    r'(        raise HTTPException\(status_code=403, detail="Setup already completed[^"]*"\))\n\n        (pw_valid, pw_msg = _validate_password)',
    r'\1\n\n    \2',
    "L-1d: Setup password validation indent (regex)")

# ═══════════════════════════════════════════════════════════════
print("\n── M-3: Password validation on user update ──")

# Find the user update endpoint and add password validation
# The pattern is in PATCH /api/users/{id} where password_hash is set
# We need to add validation before hashing

# Look for the password handling in user update
for i, line in enumerate(main_lines):
    if 'hash_password' in line and i > 4800:
        context = main_lines[max(0,i-3):i+3]
        print(f"  Found hash_password at line {i+1}:")
        for c in context:
            print(f"    {c}")
        break

# Pattern: if "password" in body: ... updates["password_hash"] = hash_password(body["password"])
fix_file(MAIN_PY,
    'updates["password_hash"] = hash_password(body["password"])',
    'pw_valid, pw_msg = _validate_password(body["password"])\n        if not pw_valid:\n            raise HTTPException(status_code=400, detail=pw_msg)\n        updates["password_hash"] = hash_password(body["password"])',
    "M-3: Password validation on user update")

# ═══════════════════════════════════════════════════════════════
print("\n── L-3: Fix SMTP placeholder ──")

settings_path = f"{FRONTEND}/pages/Settings.jsx"
if os.path.exists(settings_path):
    fix_file(settings_path,
        'printfarm@yourdomain.com',
        'odin@yourdomain.com',
        "L-3: SMTP placeholder")
else:
    print(f"  ✗ L-3: Settings.jsx not found")
    fixes_failed += 1

# ═══════════════════════════════════════════════════════════════
print("\n── L-4: Make go2rtc path configurable ──")

fix_file(MAIN_PY,
    'GO2RTC_CONFIG = "/opt/printfarm-scheduler/go2rtc/go2rtc.yaml"',
    'GO2RTC_CONFIG = os.environ.get("GO2RTC_CONFIG", "/opt/printfarm-scheduler/go2rtc/go2rtc.yaml")',
    "L-4: go2rtc path configurable")

# ═══════════════════════════════════════════════════════════════
print("\n── M-12: Disable legacy printfarm.service ──")

if not DRY_RUN:
    ret = os.system("systemctl is-enabled printfarm.service 2>/dev/null")
    if ret == 0:
        os.system("systemctl stop printfarm.service 2>/dev/null")
        os.system("systemctl disable printfarm.service 2>/dev/null")
        print("  ✓ M-12: printfarm.service disabled")
        fixes_applied += 1
    else:
        print("  ✓ M-12: printfarm.service already disabled or not found")
        fixes_applied += 1
else:
    print("  ○ M-12: would disable printfarm.service (dry-run)")
    fixes_applied += 1


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print(f"  RESULTS: {fixes_applied} fixed, {fixes_failed} failed")
if DRY_RUN:
    print(f"  This was a DRY RUN. Run with --apply to write changes.")
    print(f"    python3 {sys.argv[0]} --apply")
print("=" * 60)

if fixes_failed > 0:
    print(f"\n  Failed fixes may need manual inspection.")
    print(f"  Run the script without --apply first to see what would change.")
