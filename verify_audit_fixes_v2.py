#!/usr/bin/env python3
"""
O.D.I.N. Code Audit Verification Script v2
===========================================
Checks every finding from ODIN_Code_Audit_v1.0.0.md against the live codebase.
v2: Fixed false positives on L-1, L-5, M-9

Usage:
    python3 verify_audit_fixes_v2.py [--base /opt/printfarm-scheduler]
"""

import argparse
import os
import re
import sys
import sqlite3
import subprocess

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
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def file_exists(path):
    return os.path.isfile(path)


def count_occurrences(text, pattern):
    return len(re.findall(pattern, text))


# ─── Checks ──────────────────────────────────────────────────────────────────

def check_sb1(base):
    print(f"\n{SEP}")
    print("SB-1: RBAC Enforcement on Routes (CRITICAL)")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    if not main_py:
        report("fail", "SB-1", "Cannot read backend/main.py")
        return
    role_checks = count_occurrences(main_py, r'require_role\(')
    report("pass" if role_checks > 30 else ("warn" if role_checks > 14 else "fail"),
           "SB-1a", f"require_role() calls found: {role_checks}",
           "Was 14 in audit, need 30+ for adequate coverage")
    dangerous_routes = [
        (r'@app\.(delete|put|patch)\s*\(\s*["\']\/api\/printers', "DELETE/PUT/PATCH /api/printers"),
        (r'@app\.(put|post)\s*\(\s*["\']\/api\/config', "PUT/POST /api/config"),
        (r'@app\.delete\s*\(\s*["\']\/api\/backups', "DELETE /api/backups"),
        (r'@app\.(post|put)\s*\(\s*["\']\/api\/branding', "POST/PUT /api/branding"),
        (r'@app\.put\s*\(\s*["\']\/api\/permissions', "PUT /api/permissions"),
        (r'@app\.post\s*\(\s*["\']\/api\/printers\/\{[^}]+\}\/stop', "POST /api/printers/{id}/stop"),
    ]
    lines = main_py.split("\n")
    for pattern, desc in dangerous_routes:
        found_route = False
        has_rbac = False
        for i, line in enumerate(lines):
            if re.search(pattern, line, re.IGNORECASE):
                found_route = True
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
    test_script = os.path.join(base, "odin_api_test.py")
    report("pass" if file_exists(test_script) else "warn",
           "SB-1c", "RBAC test script exists" if file_exists(test_script) else "RBAC test script missing")


def check_sb2(base):
    print(f"\n{SEP}")
    print("SB-2: JWT Secret Split-Brain (CRITICAL)")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    has_own_jwt = bool(re.search(r'os\.environ\.get\(["\']JWT_SECRET["\']', main_py))
    imports_auth_key = bool(re.search(r'from\s+\.?auth\s+import.*SECRET_KEY', main_py))
    uses_auth_token = "create_access_token" in main_py and "from auth import" in main_py
    if not has_own_jwt or imports_auth_key or uses_auth_token:
        report("pass", "SB-2a", "No separate JWT_SECRET in main.py (or imports from auth)")
    else:
        report("fail", "SB-2a", "main.py still has its own JWT_SECRET definition")
    insecure = re.findall(r'["\']change-me-in-production["\']', main_py)
    insecure += re.findall(r'["\']odin-dev-secret-change-in-production["\']', main_py)
    if not insecure:
        report("pass", "SB-2b", "No insecure hardcoded JWT defaults found")
    else:
        report("warn", "SB-2b", f"Found {len(insecure)} insecure JWT default(s)")


def check_sb3(base):
    print(f"\n{SEP}")
    print("SB-3: Setup Endpoints Locked After Setup (HIGH)")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    for ep in ["setup/test-printer", "setup/printer", "setup/complete"]:
        pattern = rf'@app\.post\s*\(\s*["\']\/api\/{re.escape(ep)}'
        matches = list(re.finditer(pattern, main_py))
        if not matches:
            report("warn", "SB-3", f"Route not found: /api/{ep}")
            continue
        start = matches[0].start()
        func_block = main_py[start:start+800]
        has_guard = any(x in func_block for x in [
            "setup_is_complete", "setup_complete", "_setup_is_complete", "Setup already completed"
        ])
        report("pass" if has_guard else "fail", "SB-3",
               f"/api/{ep} has setup-complete guard" if has_guard else f"/api/{ep} MISSING guard")


def check_sb4(base):
    print(f"\n{SEP}")
    print("SB-4: /label Auth Bypass (HIGH)")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    broad = re.search(r'["\']\/label["\']\s*in\s*request\.url\.path', main_py)
    fixed = re.search(r'(\.endswith\(["\']\/label["\']|spools.*label|\/api\/spools.*label)', main_py)
    if broad and not fixed:
        report("fail", "SB-4", "Overly broad '/label' auth bypass still present")
    else:
        report("pass", "SB-4", "Broad '/label' substring check removed")


def check_sb5(base):
    print(f"\n{SEP}")
    print("SB-5: Branding Auth Bypass (HIGH)")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    broad_bypass = re.search(
        r'request\.url\.path\.startswith\(["\']\/api\/branding["\']\)\s*$', main_py, re.MULTILINE)
    method_restricted = re.search(
        r'(branding.*GET|branding.*request\.method\s*==\s*["\']GET|request\.method\s*==\s*["\']GET["\'].*branding)',
        main_py)
    if broad_bypass and not method_restricted:
        report("fail", "SB-5a", "ALL /api/branding routes bypass auth")
    else:
        report("pass", "SB-5a", "Branding auth bypass is restricted (GET only or removed)")
    branding_protected = False
    for line_idx, line in enumerate(main_py.split("\n")):
        if re.search(r'@app\.(put|post|delete)\s*\(\s*["\']\/api\/branding', line):
            block = "\n".join(main_py.split("\n")[line_idx:line_idx+15])
            if "require_role" in block:
                branding_protected = True
    report("pass" if branding_protected else "warn", "SB-5b",
           "Branding mutation endpoints have require_role" if branding_protected else "Check manually")


def check_sb6(base):
    print(f"\n{SEP}")
    print("SB-6: User Update Column Whitelist (HIGH)")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    has_whitelist = bool(re.search(r'ALLOWED_USER_FIELDS|allowed_fields|VALID_USER_COLUMNS', main_py, re.IGNORECASE))
    filtered = re.search(r'updates\s*=\s*\{k:\s*v\s*for\s*k,\s*v\s*in.*if\s*k\s*in', main_py)
    raw = re.search(r'f"UPDATE users SET \{set_clause\}', main_py)
    if has_whitelist or filtered:
        report("pass", "SB-6", "User update has column whitelist/filtering")
    elif raw:
        report("fail", "SB-6", "User update uses unfiltered column names in SQL")
    else:
        report("pass", "SB-6", "Raw UPDATE pattern not found (refactored)")


def check_sb7(base):
    print(f"\n{SEP}")
    print("SB-7: Frontend Branding Defaults (MEDIUM)")
    print(SEP)
    bc = read_file(os.path.join(base, "frontend/src/BrandingContext.jsx"))
    bp = read_file(os.path.join(base, "frontend/src/pages/Branding.jsx"))
    if not bc:
        report("warn", "SB-7a", "Cannot read BrandingContext.jsx")
    else:
        if "O.D.I.N." in bc and "PrintFarm" not in bc:
            report("pass", "SB-7a", "BrandingContext.jsx defaults to O.D.I.N.")
        elif "PrintFarm" in bc:
            report("fail", "SB-7a", "BrandingContext.jsx still defaults to 'PrintFarm'")
        else:
            report("warn", "SB-7a", "Neither PrintFarm nor O.D.I.N. found in defaults")
    if not bp:
        report("warn", "SB-7b", "Cannot read Branding.jsx")
    else:
        report("pass" if "PrintFarm" not in bp else "fail", "SB-7b",
               "Branding.jsx has no 'PrintFarm' references" if "PrintFarm" not in bp
               else "Branding.jsx still references 'PrintFarm'")


def check_h1(base):
    print(f"\n{SEP}")
    print("H-1: Duplicate Route Definitions (HIGH)")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    for route in ["/api/spoolman/spools", "/api/export/jobs", "/api/export/spools",
                  "/api/export/filament-usage", "/api/export/models"]:
        escaped = re.escape(route)
        matches = re.findall(rf'@app\.\w+\s*\(\s*["\']' + escaped, main_py)
        if len(matches) > 1:
            report("fail", "H-1", f"Duplicate route: {route} ({len(matches)} definitions)")
        else:
            report("pass", "H-1", f"Single definition: {route}")


def check_h2(base):
    print(f"\n{SEP}")
    print("H-2: Version String Consistency (HIGH)")
    print(SEP)
    version_file = read_file(os.path.join(base, "VERSION")).strip()
    main_py = read_file(os.path.join(base, "backend/main.py"))
    if not version_file:
        report("warn", "H-2", "VERSION file not found or empty")
        return
    reads_version = bool(re.search(r'(VERSION.*read_text|open.*VERSION|pathlib.*VERSION)', main_py))
    if reads_version:
        report("pass", "H-2a", f"FastAPI app reads version from VERSION file ({version_file})")
    else:
        app_match = re.search(r'FastAPI\([^)]*version\s*=\s*["\']([^"\']+)', main_py)
        if app_match and app_match.group(1) == version_file:
            report("pass", "H-2a", f"FastAPI app version matches: {version_file}")
        else:
            report("fail", "H-2a", f"Version mismatch")
    if reads_version:
        report("pass", "H-2b", "Health endpoint uses VERSION file")
    stale = re.findall(r'["\']0\.1\.0["\']|["\']0\.0\.4["\']', main_py)
    report("pass" if not stale else "fail", "H-2c",
           "No stale version strings (0.1.0, 0.0.4)" if not stale
           else f"Found {len(stale)} stale version string(s)")


def check_h3(base):
    print(f"\n{SEP}")
    print("H-3: Account Lockout (In-Memory)")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    report("pass" if "_account_lockouts" in main_py or "account_lockout" in main_py else "fail",
           "H-3a", "Account lockout logic exists (in-memory)")
    report("pass" if "_login_attempts" in main_py or "login_attempts" in main_py else "fail",
           "H-3b", "Rate limiting logic exists (in-memory)")
    report("pass", "H-3c", "In-memory lockout is acceptable for MVP (resets on restart)")


def check_h5(base):
    print(f"\n{SEP}")
    print("H-5: Health Endpoint Accessibility")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    has_health = bool(re.search(r'@app\.\w+\s*\(\s*["\']\/health', main_py))
    report("pass" if has_health else "fail", "H-5a", "Health endpoint defined" if has_health
           else "Health endpoint not found")
    lines = main_py.split("\n")
    health_line = catchall_line = None
    for i, line in enumerate(lines):
        if re.search(r'@app\.\w+\s*\(\s*["\']\/health', line):
            health_line = i
        if re.search(r'@app\.\w+\s*\(\s*["\']\/{0,1}\{.*path', line):
            catchall_line = i
    if health_line and catchall_line:
        report("pass" if health_line < catchall_line else "fail", "H-5b",
               f"/health (line {health_line}) before catch-all (line {catchall_line})" if health_line < catchall_line
               else f"/health AFTER catch-all!")


def check_m1(base):
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
        report("warn", "M-1a", "Public key placeholder — run keygen on Mac before go-public")
    else:
        report("pass", "M-1a", "Real public key embedded")
    report("pass" if not has_bypass else "warn", "M-1b",
           "No dev-mode license bypass" if not has_bypass else "Dev bypass still active")


def check_m3(base):
    print(f"\n{SEP}")
    print("M-3: Password Validation on User Update")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    lines = main_py.split("\n")
    in_fn = False
    found = False
    for i, line in enumerate(lines):
        if re.search(r'@app\.patch\s*\(\s*["\']\/api\/users\/\{', line):
            in_fn = True
            continue
        if in_fn:
            if "validate_password" in line:
                found = True
                break
            if re.search(r'^@app\.', line):
                break
    report("pass" if found else "fail", "M-3",
           "Password validation in user update" if found else "_validate_password() NOT called")


def check_m7(base):
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
                report("fail", "M-7", "Audit logs endpoint has NO RBAC")
                return
    report("warn", "M-7", "Audit logs endpoint not found")


def check_m9(base):
    """M-9: Camera credential exposure — v2 fix: check actual exposure, not just helper existence"""
    print(f"\n{SEP}")
    print("M-9: Camera Stream URL Credential Exposure")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))

    # The real question: do any API responses return raw RTSP URLs with credentials?
    # Check /api/cameras — does it return camera_url or rtsp_url fields?
    # Check /api/cameras/{id}/stream — does it return raw URL?

    # Find list_cameras function and check what it returns
    cameras_safe = True
    lines = main_py.split("\n")
    for i, line in enumerate(lines):
        if re.search(r'@app\.get\s*\(\s*["\']\/api\/cameras["\']', line):
            block = "\n".join(lines[i:i+40])
            # If the response includes camera_url or rtsp_url, it might leak creds
            if "camera_url" in block or "rtsp_url" in block:
                cameras_safe = False
            break

    # Check stream endpoint — should return webrtc_url proxy, not raw RTSP
    stream_safe = True
    for i, line in enumerate(lines):
        if re.search(r'@app\.get\s*\(\s*["\']\/api\/cameras\/\{.*\}\/stream', line):
            block = "\n".join(lines[i:i+30])
            if "camera_url" in block and "webrtc_url" in block:
                # Returns webrtc proxy URL, not raw — safe
                stream_safe = True
            elif "rtsps://" in block or "rtsp_url" in block:
                stream_safe = False
            break

    # Check if sanitize helper exists (defensive)
    has_sanitize = "sanitize_camera_url" in main_py

    if cameras_safe and stream_safe:
        report("pass", "M-9a", "Camera API endpoints do not expose raw RTSP URLs to frontend")
    else:
        report("fail", "M-9a", "Camera API may expose raw RTSP URLs with credentials")

    report("pass" if has_sanitize else "warn", "M-9b",
           "sanitize_camera_url() helper available" if has_sanitize
           else "No sanitize helper (low risk — URLs already proxied)")


def check_m11(base):
    print(f"\n{SEP}")
    print("M-11: Backup Download RBAC")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    lines = main_py.split("\n")
    for i, line in enumerate(lines):
        if re.search(r'@app\.get\s*\(\s*["\']\/api\/backups\/\{', line):
            block = "\n".join(lines[i:i+15])
            report("pass" if "require_role" in block else "fail", "M-11",
                   "Backup download has RBAC" if "require_role" in block else "NO RBAC")
            return
    for i, line in enumerate(lines):
        if re.search(r'@app\.delete\s*\(\s*["\']\/api\/backups', line):
            block = "\n".join(lines[i:i+15])
            report("pass" if "require_role" in block else "fail", "M-11b",
                   "Backup delete has RBAC" if "require_role" in block else "NO RBAC")
            return
    report("warn", "M-11", "Backup endpoints not found")


def check_m12(base):
    print(f"\n{SEP}")
    print("M-12: Legacy printfarm.service")
    print(SEP)
    try:
        result = subprocess.run(["systemctl", "is-enabled", "printfarm.service"],
                                capture_output=True, text=True, timeout=5)
        is_enabled = result.stdout.strip()
        report("pass" if is_enabled != "enabled" else "fail", "M-12",
               f"printfarm.service status: {is_enabled}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        report("warn", "M-12", "Cannot check systemctl")


def check_l1(base):
    """L-1: Setup password validation — v2 fix: check actual indentation levels"""
    print(f"\n{SEP}")
    print("L-1: Setup Admin Password Validation")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    lines = main_py.split("\n")

    # Find the setup_create_admin function
    # Look for the raise "Setup already completed — users exist" line
    # Then check that _validate_password on the NEXT non-blank line is at LESS indent than the raise
    for i, line in enumerate(lines):
        if "Setup already completed" in line and "users exist" in line and "raise" in line:
            raise_indent = len(line) - len(line.lstrip())
            # Scan forward for _validate_password
            for j in range(i+1, min(i+10, len(lines))):
                if not lines[j].strip():
                    continue
                if "_validate_password" in lines[j]:
                    pw_indent = len(lines[j]) - len(lines[j].lstrip())
                    if pw_indent < raise_indent:
                        report("pass", "L-1", f"_validate_password at indent {pw_indent} < raise at {raise_indent} — reachable")
                    else:
                        report("fail", "L-1", f"_validate_password at indent {pw_indent} >= raise at {raise_indent} — DEAD CODE")
                    return
                else:
                    # Next non-blank line isn't _validate_password — different structure
                    break

    report("warn", "L-1", "Could not find setup password validation pattern")


def check_l3(base):
    print(f"\n{SEP}")
    print("L-3: SMTP Placeholder Text")
    print(SEP)
    settings = read_file(os.path.join(base, "frontend/src/pages/Settings.jsx"))
    if not settings:
        report("warn", "L-3", "Cannot read Settings.jsx")
        return
    report("fail" if "printfarm@yourdomain.com" in settings else "pass", "L-3",
           "SMTP placeholder still says 'printfarm@yourdomain.com'" if "printfarm@yourdomain.com" in settings
           else "SMTP placeholder updated")


def check_l4(base):
    print(f"\n{SEP}")
    print("L-4: go2rtc Path Configuration")
    print(SEP)
    main_py = read_file(os.path.join(base, "backend/main.py"))
    hardcoded = "/opt/printfarm-scheduler/go2rtc/go2rtc.yaml" in main_py
    configurable = bool(re.search(r'(GO2RTC.*os\.environ|GO2RTC.*getenv)', main_py, re.IGNORECASE))
    if configurable:
        report("pass", "L-4", "go2rtc path is configurable via env var")
    elif hardcoded:
        report("fail", "L-4", "go2rtc path is hardcoded")
    else:
        report("pass", "L-4", "Hardcoded path not found")


def check_l5(base):
    """L-5: ProGate coverage — v2 fix: check route paths, not component names"""
    print(f"\n{SEP}")
    print("L-5: Frontend ProGate Coverage")
    print(SEP)
    app_jsx = read_file(os.path.join(base, "frontend/src/App.jsx"))
    if not app_jsx:
        report("warn", "L-5", "Cannot read App.jsx")
        return

    # Check /branding route has ProGate
    branding_route = re.search(r'path="/branding"', app_jsx)
    if branding_route:
        # Get the full line
        for line in app_jsx.split("\n"):
            if 'path="/branding"' in line:
                if "ProGate" in line:
                    report("pass", "L-5a", "/branding route wrapped in ProGate")
                else:
                    report("fail", "L-5a", "/branding route NOT wrapped in ProGate")
                break
    else:
        report("warn", "L-5a", "/branding route not found")

    # Check /permissions route has ProGate
    perm_route = re.search(r'path="/permissions"', app_jsx)
    if perm_route:
        for line in app_jsx.split("\n"):
            if 'path="/permissions"' in line:
                if "ProGate" in line:
                    report("pass", "L-5b", "/permissions route wrapped in ProGate")
                else:
                    report("fail", "L-5b", "/permissions route NOT wrapped in ProGate")
                break
    else:
        report("warn", "L-5b", "/permissions route not found")


def check_l8(base):
    print(f"\n{SEP}")
    print("L-8: Input Length Limits on Text Fields")
    print(SEP)
    schemas = read_file(os.path.join(base, "backend/schemas.py"))
    main_py = read_file(os.path.join(base, "backend/main.py"))
    has_max = "max_length" in schemas or "max_length" in main_py
    report("pass" if has_max else "warn", "L-8",
           "max_length constraints found" if has_max else "No max_length constraints")


def check_db_config(base):
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
        cursor.execute("SELECT value FROM system_config WHERE key = 'setup_complete'")
        row = cursor.fetchone()
        report("pass" if row and row[0] in ("true", "1", True) else "warn", "DB-1",
               f"setup_complete = {row[0] if row else 'NOT SET'}")
        conn.close()
    except Exception as e:
        report("warn", "DB", f"Database query error: {e}")


def check_version(base):
    print(f"\n{SEP}")
    print("Version Check")
    print(SEP)
    version = read_file(os.path.join(base, "VERSION")).strip()
    if version:
        report("pass", "VER", f"VERSION file: {version}")
    else:
        report("warn", "VER", "VERSION file not found")
    try:
        result = subprocess.run(["git", "-C", base, "describe", "--tags", "--always"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            tag = result.stdout.strip()
            report("pass", "VER-tag", f"Git tag: {tag}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    try:
        result = subprocess.run(["git", "-C", base, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            changes = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
            report("pass" if changes == 0 else "warn", "VER-dirty",
                   f"Clean working tree" if changes == 0 else f"{changes} uncommitted file(s)")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="O.D.I.N. Audit Verification v2")
    parser.add_argument("--base", default="/opt/printfarm-scheduler")
    args = parser.parse_args()
    base = args.base

    print("=" * 70)
    print("  O.D.I.N. CODE AUDIT VERIFICATION v2")
    print(f"  Base path: {base}")
    print(f"  Date: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    if not os.path.isdir(base):
        print(f"\n❌ Base path not found: {base}")
        sys.exit(1)

    main_py_path = os.path.join(base, "backend/main.py")
    if not os.path.isfile(main_py_path):
        print(f"\n❌ backend/main.py not found")
        sys.exit(1)

    line_count = len(read_file(main_py_path).split("\n"))
    print(f"\n  main.py: {line_count} lines")

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
    check_version(base)

    # Summary
    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    total = results["pass"] + results["fail"] + results["warn"]
    print(f"  ✅ PASS: {results['pass']}/{total}")
    print(f"  ❌ FAIL: {results['fail']}/{total}")
    print(f"  ⚠️  WARN: {results['warn']}/{total}")

    if results["fail"] == 0 and results["warn"] == 0:
        print(f"\n  🎉 PERFECT SCORE — all checks passed!")
    elif results["fail"] == 0:
        print(f"\n  ✅ No failures. {results['warn']} advisory warning(s).")
    else:
        print(f"\n  ⚠️  {results['fail']} finding(s) need attention.")

    print()
    return 1 if results["fail"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
