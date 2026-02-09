#!/usr/bin/env python3
"""fix_audit_final2.py - Fix M-7 and L-1"""
import re, sys, shutil
from datetime import datetime

DRY_RUN = "--dry-run" in sys.argv
MAIN_PY = "/opt/printfarm-scheduler/backend/main.py"

if not DRY_RUN:
    backup = MAIN_PY + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(MAIN_PY, backup)
    print(f"Backup: {backup}")

with open(MAIN_PY, "r") as f:
    content = f.read()
original = content
fixes = []

# M-7: Add require_role("admin") to audit logs endpoint
audit_pat = re.compile(r'(@app\.get\s*\(\s*["\']\/api\/audit-logs["\'][^)]*\))\s*\n((?:async\s+)?def\s+\w+\s*\()')
match = audit_pat.search(content)
if match:
    ctx = content[match.start():match.start()+500]
    if "require_role" not in ctx:
        start = match.start(2)
        depth = 0
        i = start
        while i < len(content):
            if content[i] == '(': depth += 1
            elif content[i] == ')':
                depth -= 1
                if depth == 0:
                    params = content[start:i]
                    ins = ', current_user: dict = Depends(require_role("admin"))' if not params.strip().endswith('(') else 'current_user: dict = Depends(require_role("admin"))'
                    content = content[:i] + ins + content[i:]
                    fixes.append("M-7: Added require_role('admin') to GET /api/audit-logs")
                    break
            i += 1
    else:
        print("M-7: already fixed")
else:
    print("M-7: endpoint not found")

# L-1: Fix setup password validation indentation
lines = content.split("\n")
fixed = []
i = 0
l1_done = False
while i < len(lines):
    line = lines[i]
    if "Setup already completed" in line and "raise" in line:
        fixed.append(line)
        ri = len(line) - len(line.lstrip())
        j = i + 1
        while j < len(lines):
            nl = lines[j]
            if not nl.strip():
                fixed.append(nl); j += 1; continue
            ni = len(nl) - len(nl.lstrip())
            if "_validate_password" in nl and ni >= ri:
                ti = max(ri - 4, 0)
                fixed.append(" " * ti + nl.lstrip())
                k = j + 1
                while k < len(lines):
                    bl = lines[k]
                    if not bl.strip(): fixed.append(bl); k += 1; continue
                    bi = len(bl) - len(bl.lstrip())
                    if bi >= ri:
                        fixed.append(" " * (ti + (bi - ri)) + bl.lstrip())
                        k += 1
                        if "raise" in bl: break
                    else: break
                l1_done = True
                fixes.append("L-1: Dedented _validate_password to be reachable")
                i = k; continue
            else: break
        if not l1_done: i += 1; continue
    else:
        fixed.append(line)
    i += 1
if l1_done: content = "\n".join(fixed)

if content != original:
    if DRY_RUN:
        print(f"\nDRY RUN - {len(fixes)} fix(es):")
        for f in fixes: print(f"  - {f}")
    else:
        with open(MAIN_PY, "w") as f: f.write(content)
        print(f"\nApplied {len(fixes)} fix(es):")
        for f in fixes: print(f"  - {f}")
        print("\nRestart: systemctl restart printfarm-backend")
else:
    print("\nNo changes needed")
