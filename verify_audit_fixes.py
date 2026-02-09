#!/usr/bin/env python3
"""
O.D.I.N. Code Audit Verification Script
========================================
Checks every finding from ODIN_Code_Audit_v0.19.0.md against the live codebase.

Usage:
    python3 verify_audit_fixes.py [--base /opt/printfarm-scheduler]

Run on the server. Reports PASS/FAIL/WARN for each finding.
"""

import argparse
import os
import re
import sys
import sqlite3
import subprocess

# ─── Config ───────────────────────────────────────────────────────────────────

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"
WARN = "\033[93m⚠️  WARN\033[0m"
INFO = "\033[94mℹ️  INFO\033[0m"
SEP  = "─" * 70

results = {"pass": 0, "fail": 0, "warn": 0}


def report(status, finding_id, desc, detail=""):
    tag = PASS if status == "pass" else (FAIL if status == "fail" else WARN)
    if status == "pass":
        results["pass"] += 1
    elif status == "fail":
        results["fail"] += 1
    else:
        results["warn"] += 1
    print(f"  {tag}  {finding_id}: {desc}")
    if detail:
        print(f"         {detail}")


def read_file(path):
    """Read file contents, return empty string if missing."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def file_exists(path):
    return os.path.isfile(path)


def count_occurrences(text, pattern):
    """Count regex matches in text."""
    return len(re.findall(pattern, text))


# ─── Checks ──────────────────────────────────────────────────────────────────

def check_sb1(base):
    """SB-1: RBAC enforced on routes"""
    print(f"\n{SEP}")
    print("SB-1: RBAC Enforcement on Routes (CRITICAL)")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))
    if not main_py:
        report("fail", "SB-1", "Cannot read backend/main.py")
        return

    # Count require_role occurrences
    role_checks = count_occurrences(main_py, r'require_role\(')
    report("pass" if role_checks > 30 else ("warn" if role_checks > 14 else "fail"),
           "SB-1a", f"require_role() calls found: {role_checks}",
           "Was 14 in audit, need 30+ for adequate coverage")

    # Check specific dangerous routes have RBAC
    dangerous_routes = [
        (r'@app\.(delete|put|patch)\s*\(\s*["\']\/api\/printers', "DELETE/PUT/PATCH /api/printers"),
        (r'@app\.(put|post)\s*\(\s*["\']\/api\/config', "PUT/POST /api/config"),
        (r'@app\.delete\s*\(\s*["\']\/api\/backups', "DELETE /api/backups"),
        (r'@app\.(post|put)\s*\(\s*["\']\/api\/branding', "POST/PUT /api/branding"),
        (r'@app\.put\s*\(\s*["\']\/api\/permissions', "PUT /api/permissions"),
        (r'@app\.post\s*\(\s*["\']\/api\/printers\/\{[^}]+\}\/stop', "POST /api/printers/{id}/stop"),
    ]

    # For each dangerous route pattern, check that require_role appears nearby
    lines = main_py.split("\n")
    for pattern, desc in dangerous_routes:
        found_route = False
        has_rbac = False
        for i, line in enumerate(lines):
            if re.search(pattern, line, re.IGNORECASE):
                found_route = True
                # Check the function body (next ~30 lines) for require_role
                func_block = "\n".join(lines[i:i+30])
                if "require_role" in func_block:
                    has_rbac = True
                    break
        if not found_route:
            report("warn", "SB-1b", f"Route not found: {desc}", "May have been refactored")
        elif has_rbac:
            report("pass", "SB-1b", f"RBAC on: {desc}")
        else:
            report("fail", "SB-1b", f"NO RBAC on: {desc}")

    # Check RBAC test script exists (from the session)
    test_script = os.path.join(base, "odin_api_test.py")
    report("pass" if file_exists(test_script) else "warn",
           "SB-1c", "RBAC test script exists" if file_exists(test_script) else "RBAC test script missing (odin_api_test.py)")


def check_sb2(base):
    """SB-2: JWT Secret Split-Brain"""
    print(f"\n{SEP}")
    print("SB-2: JWT Secret Split-Brain (CRITICAL)")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))
    auth_py = read_file(os.path.join(base, "backend/auth.py"))

    # Check if main.py still has its own JWT_SECRET
    has_own_jwt = bool(re.search(r'os\.environ\.get\(["\']JWT_SECRET["\']', main_py))
    # Check if main.py imports from auth
    imports_auth_key = bool(re.search(r'from\s+auth\s+import.*SECRET_KEY', main_py)) or \
                       bool(re.search(r'from\s+\.?auth\s+import.*SECRET_KEY', main_py))
    # Check if main.py uses create_access_token from auth
    uses_auth_token = "create_access_token" in main_py and "from auth import" in main_py

    # Also check: does the OIDC callback use the same secret?
    # Look for the OIDC callback and see what secret it uses
    oidc_section = ""
    in_oidc = False
    for line in main_py.split("\n"):
        if "oidc" in line.lower() and ("callback" in line.lower() or "jwt" in line.lower()):
            in_oidc = True
        if in_oidc:
            oidc_section += line + "\n"
            if len(oidc_section.split("\n")) > 20:
                break

    if not has_own_jwt or imports_auth_key or uses_auth_token:
        report("pass", "SB-2a", "No separate JWT_SECRET in main.py (or imports from auth)")
    else:
        report("fail", "SB-2a", "main.py still has its own JWT_SECRET definition",
               "Should import SECRET_KEY from auth.py")

    # Check for hardcoded insecure defaults
    insecure_defaults = re.findall(r'["\']change-me-in-production["\']', main_py)
    insecure_defaults += re.findall(r'["\']odin-dev-secret-change-in-production["\']', main_py)
    if not insecure_defaults:
        report("pass", "SB-2b", "No insecure hardcoded JWT defaults found")
    else:
        report("warn", "SB-2b", f"Found {len(insecure_defaults)} insecure JWT default(s)",
               "OK for dev, must be env-var overridden in production")


def check_sb3(base):
    """SB-3: Setup Endpoints Locked After Setup"""
    print(f"\n{SEP}")
    print("SB-3: Setup Endpoints Locked After Setup (HIGH)")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    setup_endpoints = ["setup/test-printer", "setup/printer", "setup/complete"]
    for ep in setup_endpoints:
        # Find the route definition and check for setup_complete guard
        pattern = rf'@app\.post\s*\(\s*["\']\/api\/{re.escape(ep)}'
        matches = list(re.finditer(pattern, main_py))
        if not matches:
            report("warn", "SB-3", f"Route not found: /api/{ep}")
            continue

        # Check the function body for setup guard
        start = matches[0].start()
        func_block = main_py[start:start+800]
        has_guard = ("setup_is_complete" in func_block or
                     "setup_complete" in func_block or
                     "_setup_is_complete" in func_block or
                     "Setup already completed" in func_block)
        report("pass" if has_guard else "fail",
               "SB-3", f"/api/{ep} has setup-complete guard" if has_guard
               else f"/api/{ep} MISSING setup-complete guard")


def check_sb4(base):
    """SB-4: /label Auth Bypass"""
    print(f"\n{SEP}")
    print("SB-4: /label Auth Bypass (HIGH)")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    # Check for the overly broad pattern
    broad_pattern = re.search(r'["\']\/label["\']\s*in\s*request\.url\.path', main_py)
    # Check for fixed pattern (scoped to spools or exact endpoint match)
    fixed_pattern = re.search(r'(\.endswith\(["\']\/label["\']|spools.*label|\/api\/spools.*label)', main_py)

    if broad_pattern and not fixed_pattern:
        report("fail", "SB-4", "Overly broad '/label' auth bypass still present",
               "Any URL containing '/label' bypasses auth")
    elif not broad_pattern:
        report("pass", "SB-4", "Broad '/label' substring check removed")
    else:
        report("pass", "SB-4", "Label auth bypass is properly scoped")


def check_sb5(base):
    """SB-5: Branding Endpoints Bypass Auth"""
    print(f"\n{SEP}")
    print("SB-5: Branding Auth Bypass (HIGH)")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    # Check if ALL branding routes bypass auth
    broad_bypass = re.search(
        r'request\.url\.path\.startswith\(["\']\/api\/branding["\']\)\s*$',
        main_py, re.MULTILINE
    )
    # Check for method-restricted bypass (GET only)
    method_restricted = re.search(
        r'(branding.*GET|branding.*request\.method\s*==\s*["\']GET|'
        r'request\.method\s*==\s*["\']GET["\'].*branding)',
        main_py
    )

    # Also check: do PUT/POST/DELETE branding have require_role?
    branding_mutations_protected = False
    for line_idx, line in enumerate(main_py.split("\n")):
        if re.search(r'@app\.(put|post|delete)\s*\(\s*["\']\/api\/branding', line):
            block = "\n".join(main_py.split("\n")[line_idx:line_idx+15])
            if "require_role" in block:
                branding_mutations_protected = True

    if broad_bypass and not method_restricted:
        report("fail", "SB-5a", "ALL /api/branding routes bypass auth (including PUT/POST/DELETE)")
    else:
        report("pass", "SB-5a", "Branding auth bypass is restricted (GET only or removed)")

    report("pass" if branding_mutations_protected else "warn",
           "SB-5b", "Branding mutation endpoints have require_role" if branding_mutations_protected
           else "Branding mutation endpoints may lack RBAC (check manually)")


def check_sb6(base):
    """SB-6: User Update Column Whitelist"""
    print(f"\n{SEP}")
    print("SB-6: User Update Column Whitelist (HIGH)")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    # Check for ALLOWED_USER_FIELDS or similar whitelist
    has_whitelist = bool(re.search(r'ALLOWED_USER_FIELDS|allowed_fields|VALID_USER_COLUMNS', main_py, re.IGNORECASE))

    # Check if the raw column-name-in-SQL pattern is still there without filtering
    raw_pattern = re.search(r'f"UPDATE users SET \{set_clause\}', main_py)
    filtered_before = re.search(r'updates\s*=\s*\{k:\s*v\s*for\s*k,\s*v\s*in.*if\s*k\s*in', main_py)

    if has_whitelist or filtered_before:
        report("pass", "SB-6", "User update has column whitelist/filtering")
    elif raw_pattern:
        report("fail", "SB-6", "User update uses unfiltered column names in SQL",
               "Attacker could inject column names via PATCH /api/users/{id}")
    else:
        report("pass", "SB-6", "Raw UPDATE pattern not found (may have been refactored)")


def check_sb7(base):
    """SB-7: Frontend Branding Defaults"""
    print(f"\n{SEP}")
    print("SB-7: Frontend Branding Defaults (MEDIUM)")
    print(SEP)

    bc = read_file(os.path.join(base, "frontend/src/BrandingContext.jsx"))
    bp = read_file(os.path.join(base, "frontend/src/pages/Branding.jsx"))

    if not bc:
        report("warn", "SB-7a", "Cannot read BrandingContext.jsx")
    else:
        has_printfarm = "PrintFarm" in bc
        has_odin = "O.D.I.N." in bc
        if has_odin and not has_printfarm:
            report("pass", "SB-7a", "BrandingContext.jsx defaults to O.D.I.N.")
        elif has_printfarm:
            report("fail", "SB-7a", "BrandingContext.jsx still defaults to 'PrintFarm'")
        else:
            report("warn", "SB-7a", "Neither PrintFarm nor O.D.I.N. found in defaults")

    if not bp:
        report("warn", "SB-7b", "Cannot read Branding.jsx")
    else:
        has_printfarm = "PrintFarm" in bp
        if not has_printfarm:
            report("pass", "SB-7b", "Branding.jsx has no 'PrintFarm' references")
        else:
            report("fail", "SB-7b", "Branding.jsx still references 'PrintFarm'")


def check_h1(base):
    """H-1: Duplicate Route Definitions"""
    print(f"\n{SEP}")
    print("H-1: Duplicate Route Definitions (HIGH)")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    dup_routes = [
        "/api/spoolman/spools",
        "/api/export/jobs",
        "/api/export/spools",
        "/api/export/filament-usage",
        "/api/export/models",
    ]

    for route in dup_routes:
        escaped = re.escape(route)
        # Count how many times this route is defined as a decorator
        matches = re.findall(rf'@app\.\w+\s*\(\s*["\']' + escaped, main_py)
        if len(matches) > 1:
            report("fail", "H-1", f"Duplicate route: {route} ({len(matches)} definitions)")
        elif len(matches) == 1:
            report("pass", "H-1", f"Single definition: {route}")
        else:
            report("pass", "H-1", f"Route removed or refactored: {route}")


def check_h2(base):
    """H-2: Version String Mismatch"""
    print(f"\n{SEP}")
    print("H-2: Version String Consistency (HIGH)")
    print(SEP)

    version_file = read_file(os.path.join(base, "VERSION")).strip()
    main_py = read_file(os.path.join(base, "backend/main.py"))

    if not version_file:
        report("warn", "H-2", "VERSION file not found or empty")
        return

    report("info", "H-2", f"VERSION file: {version_file}")

    # Check if main.py reads from VERSION file
    reads_version_file = bool(re.search(r'(VERSION.*read_text|open.*VERSION|pathlib.*VERSION)', main_py))

    # Check FastAPI app version
    app_version_match = re.search(r'FastAPI\([^)]*version\s*=\s*["\']([^"\']+)', main_py)
    if app_version_match:
        app_ver = app_version_match.group(1)
        if app_ver == version_file or reads_version_file:
            report("pass", "H-2a", f"FastAPI app version: {app_ver}")
        else:
            report("fail", "H-2a", f"FastAPI app version mismatch: {app_ver} (expected {version_file})")
    elif reads_version_file:
        report("pass", "H-2a", "FastAPI app reads version from VERSION file")

    # Check health endpoint version
    health_match = re.search(r'"version"\s*:\s*["\']([^"\']+)["\']', main_py[:15000])
    if health_match:
        health_ver = health_match.group(1)
        if health_ver == version_file or reads_version_file:
            report("pass", "H-2b", f"Health endpoint version: {health_ver}")
        else:
            report("fail", "H-2b", f"Health endpoint version mismatch: {health_ver}")
    elif reads_version_file:
        report("pass", "H-2b", "Health endpoint likely uses VERSION file")

    # Check for 0.1.0 or 0.0.4 hardcoded
    stale_versions = re.findall(r'["\']0\.1\.0["\']|["\']0\.0\.4["\']', main_py)
    if not stale_versions:
        report("pass", "H-2c", "No stale version strings (0.1.0, 0.0.4) found")
    else:
        report("fail", "H-2c", f"Found {len(stale_versions)} stale version string(s)")


def check_h3(base):
    """H-3: Account Lockout Behavior"""
    print(f"\n{SEP}")
    print("H-3: Account Lockout (Documentation Check)")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    has_lockout_dict = "_account_lockouts" in main_py or "account_lockout" in main_py
    has_login_attempts = "_login_attempts" in main_py or "login_attempts" in main_py

    report("pass" if has_lockout_dict else "warn",
           "H-3a", "Account lockout logic exists (in-memory)" if has_lockout_dict
           else "Account lockout logic not found")
    report("pass" if has_login_attempts else "warn",
           "H-3b", "Rate limiting logic exists (in-memory)" if has_login_attempts
           else "Rate limiting logic not found")

    # Check if there's a database-backed lockout
    has_db_lockout = "is_locked" in main_py and "users" in main_py
    report("info", "H-3c",
           "Lockouts are IN-MEMORY ONLY — reset on server restart" if not has_db_lockout
           else "Database-backed lockout may exist")


def check_h5(base):
    """H-5: Health Endpoint"""
    print(f"\n{SEP}")
    print("H-5: Health Endpoint Accessibility")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    has_health = bool(re.search(r'@app\.\w+\s*\(\s*["\']\/health', main_py))
    report("pass" if has_health else "fail", "H-5", "Health endpoint defined" if has_health
           else "Health endpoint not found")

    # Check if catch-all route exists and where
    lines = main_py.split("\n")
    health_line = None
    catchall_line = None
    for i, line in enumerate(lines):
        if re.search(r'@app\.\w+\s*\(\s*["\']\/health', line):
            health_line = i
        if re.search(r'@app\.\w+\s*\(\s*["\']\/{0,1}\{.*path', line):
            catchall_line = i

    if health_line and catchall_line:
        if health_line < catchall_line:
            report("pass", "H-5b", f"/health (line {health_line}) before catch-all (line {catchall_line})")
        else:
            report("fail", "H-5b", f"/health (line {health_line}) AFTER catch-all (line {catchall_line})",
                   "Catch-all will shadow /health!")


def check_m1(base):
    """M-1: License Signature Bypass"""
    print(f"\n{SEP}")
    print("M-1: License Signature Bypass in Dev Mode")
    print(SEP)

    lm = read_file(os.path.join(base, "backend/license_manager.py"))
    if not lm:
        report("warn", "M-1", "Cannot read license_manager.py")
        return

    has_placeholder = "REPLACE_WITH_YOUR_PUBLIC_KEY" in lm
    has_bypass = re.search(r'if.*REPLACE_WITH.*return True', lm)

    if has_placeholder:
        report("warn", "M-1a", "Public key placeholder still present",
               "Must generate Ed25519 keypair before going public")
    else:
        report("pass", "M-1a", "Real public key embedded")

    if has_bypass:
        report("warn", "M-1b", "Dev-mode bypass (accept any license) still active")
    else:
        report("pass", "M-1b", "No dev-mode license bypass")


def check_m3(base):
    """M-3: Password Validation on User Update"""
    print(f"\n{SEP}")
    print("M-3: Password Validation on User Update")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    # Find the PATCH /api/users/{id} handler and check for _validate_password
    lines = main_py.split("\n")
    in_user_update = False
    found_validation = False
    for i, line in enumerate(lines):
        if re.search(r'@app\.patch\s*\(\s*["\']\/api\/users\/\{', line):
            in_user_update = True
            continue
        if in_user_update:
            if "validate_password" in line:
                found_validation = True
                break
            # Stop looking after next route decorator
            if re.search(r'^@app\.', line) and i > 0:
                break

    report("pass" if found_validation else "fail",
           "M-3", "Password validation in user update" if found_validation
           else "_validate_password() NOT called in PATCH /api/users/{id}")


def check_m7(base):
    """M-7: Audit Logs RBAC"""
    print(f"\n{SEP}")
    print("M-7: Audit Logs RBAC")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    lines = main_py.split("\n")
    for i, line in enumerate(lines):
        if re.search(r'@app\.\w+\s*\(\s*["\']\/api\/audit', line):
            block = "\n".join(lines[i:i+15])
            if "require_role" in block:
                report("pass", "M-7", "Audit logs endpoint has RBAC")
                return
            else:
                report("fail", "M-7", "Audit logs endpoint has NO RBAC",
                       "Any authenticated user can view login attempts + IPs")
                return
    report("warn", "M-7", "Audit logs endpoint not found")


def check_m11(base):
    """M-11: Backup Download Auth"""
    print(f"\n{SEP}")
    print("M-11: Backup Download RBAC")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    lines = main_py.split("\n")
    for i, line in enumerate(lines):
        if re.search(r'@app\.get\s*\(\s*["\']\/api\/backups\/\{', line):
            block = "\n".join(lines[i:i+15])
            if "require_role" in block:
                report("pass", "M-11", "Backup download has RBAC")
                return
            else:
                report("fail", "M-11", "Backup download has NO RBAC",
                       "Any authenticated user can download DB backups")
                return

    # Also check delete
    for i, line in enumerate(lines):
        if re.search(r'@app\.delete\s*\(\s*["\']\/api\/backups', line):
            block = "\n".join(lines[i:i+15])
            if "require_role" in block:
                report("pass", "M-11b", "Backup delete has RBAC")
            else:
                report("fail", "M-11b", "Backup delete has NO RBAC")
            return

    report("warn", "M-11", "Backup endpoints not found")


def check_m12(base):
    """M-12: Legacy printfarm.service"""
    print(f"\n{SEP}")
    print("M-12: Legacy printfarm.service")
    print(SEP)

    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", "printfarm.service"],
            capture_output=True, text=True, timeout=5
        )
        is_enabled = result.stdout.strip()
        if is_enabled == "enabled":
            report("fail", "M-12", "printfarm.service is still ENABLED",
                   "Run: systemctl disable printfarm.service && systemctl stop printfarm.service")
        else:
            report("pass", "M-12", f"printfarm.service status: {is_enabled}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        report("warn", "M-12", "Cannot check systemctl (not running as root or systemd not available)")


def check_l1(base):
    """L-1: Setup Password Validation Indentation"""
    print(f"\n{SEP}")
    print("L-1: Setup Admin Password Validation")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    # Find setup_create_admin function
    lines = main_py.split("\n")
    in_setup_admin = False
    found_pw_validation = False
    pw_is_reachable = True

    for i, line in enumerate(lines):
        if "setup" in line and "admin" in line and ("def " in line or "@app" in line):
            in_setup_admin = True
            continue
        if in_setup_admin:
            if "_validate_password" in line:
                found_pw_validation = True
                # Check indentation: is this inside an if-raise block?
                # Look back at previous non-empty lines
                indent = len(line) - len(line.lstrip())
                for j in range(i-1, max(i-10, 0), -1):
                    prev = lines[j]
                    if prev.strip():
                        prev_indent = len(prev) - len(prev.lstrip())
                        if "raise" in prev and prev_indent >= indent:
                            pw_is_reachable = False
                        break
                break
            if re.search(r'^def |^@app\.', line) and in_setup_admin:
                break

    if found_pw_validation and pw_is_reachable:
        report("pass", "L-1", "Password validation in setup is reachable")
    elif found_pw_validation and not pw_is_reachable:
        report("fail", "L-1", "Password validation is DEAD CODE (indented under raise)")
    else:
        report("warn", "L-1", "Password validation not found in setup admin handler")


def check_l3(base):
    """L-3: SMTP Placeholder"""
    print(f"\n{SEP}")
    print("L-3: SMTP Placeholder Text")
    print(SEP)

    settings = read_file(os.path.join(base, "frontend/src/pages/Settings.jsx"))
    if not settings:
        report("warn", "L-3", "Cannot read Settings.jsx")
        return

    has_printfarm_email = "printfarm@yourdomain.com" in settings
    report("fail" if has_printfarm_email else "pass",
           "L-3", "SMTP placeholder still says 'printfarm@yourdomain.com'" if has_printfarm_email
           else "SMTP placeholder updated")


def check_l4(base):
    """L-4: go2rtc Path"""
    print(f"\n{SEP}")
    print("L-4: go2rtc Path Configuration")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    hardcoded = "/opt/printfarm-scheduler/go2rtc/go2rtc.yaml" in main_py
    configurable = bool(re.search(r'(GO2RTC.*os\.environ|GO2RTC.*config|GO2RTC.*getenv)', main_py, re.IGNORECASE))

    if hardcoded and not configurable:
        report("fail", "L-4", "go2rtc path is hardcoded to /opt/printfarm-scheduler/",
               "Won't work in Docker")
    elif configurable:
        report("pass", "L-4", "go2rtc path is configurable")
    else:
        report("pass", "L-4", "Hardcoded path not found (may be refactored)")


def check_l5(base):
    """L-5: ProGate Coverage"""
    print(f"\n{SEP}")
    print("L-5: Frontend ProGate Coverage")
    print(SEP)

    app_jsx = read_file(os.path.join(base, "frontend/src/App.jsx"))
    if not app_jsx:
        report("warn", "L-5", "Cannot read App.jsx")
        return

    pro_pages = ["Branding", "Permissions"]
    for page in pro_pages:
        # Check if the page route has ProGate wrapper
        has_progate = bool(re.search(rf'ProGate.*{page}|{page}.*ProGate', app_jsx))
        report("pass" if has_progate else "warn",
               "L-5", f"{page} page wrapped in ProGate" if has_progate
               else f"{page} page may lack ProGate wrapper")


def check_m9(base):
    """M-9: Camera URLs expose credentials"""
    print(f"\n{SEP}")
    print("M-9: Camera Stream URL Credential Exposure")
    print(SEP)

    main_py = read_file(os.path.join(base, "backend/main.py"))

    # Check if camera endpoints strip credentials from RTSP URLs
    has_credential_stripping = bool(re.search(
        r'(strip.*access_code|replace.*bblp:|mask.*credential|rtsp_url.*sanitize)',
        main_py, re.IGNORECASE
    ))

    report("pass" if has_credential_stripping else "warn",
           "M-9", "Camera URLs have credential handling" if has_credential_stripping
           else "Camera URLs may still expose printer access codes in RTSP URLs")


def check_l8(base):
    """L-8: Input Length Limits"""
    print(f"\n{SEP}")
    print("L-8: Input Length Limits on Text Fields")
    print(SEP)

    schemas = read_file(os.path.join(base, "backend/schemas.py"))
    main_py = read_file(os.path.join(base, "backend/main.py"))

    has_max_length = "max_length" in schemas or "max_length" in main_py
    has_field_validator = "field_validator" in schemas or "validator" in schemas

    report("pass" if has_max_length else "warn",
           "L-8", "max_length constraints found in schemas" if has_max_length
           else "No max_length constraints found — unbounded text inputs possible")


def check_db_config(base):
    """Check system_config for setup_complete"""
    print(f"\n{SEP}")
    print("DB: System Config State")
    print(SEP)

    db_paths = [
        os.path.join(base, "backend/printfarm.db"),
        os.path.join(base, "data/printfarm.db"),
        "/data/printfarm.db",
    ]

    db_path = None
    for p in db_paths:
        if os.path.isfile(p):
            db_path = p
            break

    if not db_path:
        report("warn", "DB", "Database file not found")
        return

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Check setup_complete
        cursor.execute("SELECT value FROM system_config WHERE key = 'setup_complete'")
        row = cursor.fetchone()
        if row and row[0] in ("true", "1", True):
            report("pass", "DB-1", "setup_complete = true")
        else:
            report("warn", "DB-1", f"setup_complete = {row[0] if row else 'NOT SET'}")

        # Check license_tier
        cursor.execute("SELECT value FROM system_config WHERE key = 'license_tier'")
        row = cursor.fetchone()
        if row:
            report("info", "DB-2", f"license_tier = {row[0]}")
        else:
            report("info", "DB-2", "license_tier not set in system_config")

        # Count users
        cursor.execute("SELECT COUNT(*) FROM users")
        user_count = cursor.fetchone()[0]
        report("info", "DB-3", f"User count: {user_count}")

        # Check for require_job_approval
        cursor.execute("SELECT value FROM system_config WHERE key = 'require_job_approval'")
        row = cursor.fetchone()
        report("info", "DB-4", f"require_job_approval = {row[0] if row else 'NOT SET'}")

        conn.close()
    except Exception as e:
        report("warn", "DB", f"Database query error: {e}")


def check_version_file(base):
    """Check VERSION file"""
    print(f"\n{SEP}")
    print("Version Check")
    print(SEP)

    version = read_file(os.path.join(base, "VERSION")).strip()
    if version:
        report("info", "VER", f"VERSION file: {version}")
    else:
        report("warn", "VER", "VERSION file not found")

    # Check git status
    try:
        result = subprocess.run(
            ["git", "-C", base, "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            report("info", "VER", f"Git tag: {result.stdout.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    try:
        result = subprocess.run(
            ["git", "-C", base, "status", "--porcelain"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            changes = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
            report("info", "VER", f"Uncommitted changes: {changes} files")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="O.D.I.N. Audit Verification")
    parser.add_argument("--base", default="/opt/printfarm-scheduler",
                        help="Base path to the O.D.I.N. installation")
    args = parser.parse_args()

    base = args.base

    print("=" * 70)
    print("  O.D.I.N. CODE AUDIT VERIFICATION")
    print(f"  Base path: {base}")
    print(f"  Date: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # Verify base exists
    if not os.path.isdir(base):
        print(f"\n❌ Base path not found: {base}")
        print("   Use --base /path/to/printfarm-scheduler")
        sys.exit(1)

    main_py = os.path.join(base, "backend/main.py")
    if not os.path.isfile(main_py):
        print(f"\n❌ backend/main.py not found at {main_py}")
        sys.exit(1)

    line_count = len(read_file(main_py).split("\n"))
    print(f"\n  main.py: {line_count} lines")

    # ── Run all checks ──

    # Ship-blocking
    check_sb1(base)
    check_sb2(base)
    check_sb3(base)
    check_sb4(base)
    check_sb5(base)
    check_sb6(base)
    check_sb7(base)

    # High
    check_h1(base)
    check_h2(base)
    check_h3(base)
    check_h5(base)

    # Medium
    check_m1(base)
    check_m3(base)
    check_m7(base)
    check_m9(base)
    check_m11(base)
    check_m12(base)

    # Low
    check_l1(base)
    check_l3(base)
    check_l4(base)
    check_l5(base)
    check_l8(base)

    # DB & Version
    check_db_config(base)
    check_version_file(base)

    # ── Summary ──
    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    total = results["pass"] + results["fail"] + results["warn"]
    print(f"  {PASS.split('PASS')[0]}PASS: {results['pass']}/{total}")
    print(f"  {FAIL.split('FAIL')[0]}FAIL: {results['fail']}/{total}")
    print(f"  {WARN.split('WARN')[0]}WARN: {results['warn']}/{total}")

    if results["fail"] == 0:
        print(f"\n  🎉 All critical checks passed!")
    else:
        print(f"\n  ⚠️  {results['fail']} finding(s) still need attention.")

    print()
    return 1 if results["fail"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
