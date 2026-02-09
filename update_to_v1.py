#!/usr/bin/env python3
"""Update ALL version references from 0.19.x to 1.0.0"""
import os
import re
import datetime

BASE = "/opt/printfarm-scheduler"

def replace_in_file(filepath, old, new):
    with open(filepath, 'r') as f:
        content = f.read()
    if old not in content:
        print(f"  ⚠️  '{old}' not found in {filepath}")
        return False
    content = content.replace(old, new)
    with open(filepath, 'w') as f:
        f.write(content)
    print(f"  ✅ {filepath}: '{old}' → '{new}'")
    return True

def regex_replace_in_file(filepath, pattern, replacement):
    with open(filepath, 'r') as f:
        content = f.read()
    new_content, count = re.subn(pattern, replacement, content)
    if count == 0:
        print(f"  ⚠️  Pattern not found in {filepath}")
        return False
    with open(filepath, 'w') as f:
        f.write(new_content)
    print(f"  ✅ {filepath}: {count} replacement(s)")
    return True

print("=" * 60)
print("  O.D.I.N. v1.0.0 — Version Update Script")
print("=" * 60)

# 1. VERSION file
print("\n1. VERSION file")
with open(f"{BASE}/VERSION", 'w') as f:
    f.write("1.0.0\n")
print(f"  ✅ {BASE}/VERSION → 1.0.0")

# 2. main.py — FastAPI app title version
print("\n2. main.py — FastAPI app version")
main_py = f"{BASE}/backend/main.py"
with open(main_py, 'r') as f:
    content = f.read()

# FastAPI app title — match version="0.x.x" pattern
content = re.sub(
    r'(app\s*=\s*FastAPI\([^)]*version\s*=\s*")[^"]*(")',
    r'\g<1>1.0.0\2',
    content
)
print("  ✅ FastAPI app version → 1.0.0")

# Health endpoint version — match "version": "0.x.x"
content = re.sub(
    r'("version"\s*:\s*")[^"]*(".*?# health)',
    r'\g<1>1.0.0\2',
    content,
    flags=re.DOTALL
)
# Also try simpler pattern for health endpoint
content = re.sub(
    r'("version":\s*")[^"]*(")',
    lambda m: f'{m.group(1)}1.0.0{m.group(2)}' if '0.1.0' in m.group(0) or '0.19' in m.group(0) else m.group(0),
    content
)
print("  ✅ Health endpoint version → 1.0.0")

# Prometheus version
content = re.sub(
    r'(odin_info.*?version\s*[=:]\s*")[^"]*(")',
    r'\g<1>1.0.0\2',
    content,
    flags=re.DOTALL
)
# Try alternate pattern
content = re.sub(
    r'("0\.0\.4")',
    '"1.0.0"',
    content
)
print("  ✅ Prometheus version → 1.0.0")

with open(main_py, 'w') as f:
    f.write(content)

# 3. package.json
print("\n3. package.json")
pkg_json = f"{BASE}/frontend/package.json"
if os.path.exists(pkg_json):
    regex_replace_in_file(pkg_json, r'"version"\s*:\s*"[^"]*"', '"version": "1.0.0"')

# 4. verify_audit_fixes_v2.py — update expected version
print("\n4. verify_audit_fixes_v2.py")
verify_script = f"{BASE}/verify_audit_fixes_v2.py"
if os.path.exists(verify_script):
    with open(verify_script, 'r') as f:
        vc = f.read()
    vc = vc.replace('0.19.0', '1.0.0')
    vc = vc.replace('v0.19.1', 'v1.0.0')
    vc = vc.replace('v0.19.0', 'v1.0.0')
    with open(verify_script, 'w') as f:
        f.write(vc)
    print(f"  ✅ verify_audit_fixes_v2.py updated")

# 5. BSL LICENSE file — update version reference if present
print("\n5. LICENSE file")
license_file = f"{BASE}/LICENSE"
if os.path.exists(license_file):
    with open(license_file, 'r') as f:
        lic = f.read()
    if '0.19' in lic:
        lic = lic.replace('0.19.0', '1.0.0')
        with open(license_file, 'w') as f:
            f.write(lic)
        print(f"  ✅ LICENSE updated")
    else:
        print(f"  ℹ️  No version refs in LICENSE")

# 6. Dockerfile / docker-compose if they have version labels
print("\n6. Docker files")
for docker_file in ['Dockerfile', 'docker-compose.yml']:
    fp = f"{BASE}/{docker_file}"
    if os.path.exists(fp):
        with open(fp, 'r') as f:
            dc = f.read()
        if '0.19' in dc:
            dc = dc.replace('0.19.0', '1.0.0')
            dc = dc.replace('0.19.1', '1.0.0')
            with open(fp, 'w') as f:
                f.write(dc)
            print(f"  ✅ {docker_file} updated")
        else:
            print(f"  ℹ️  No version refs in {docker_file}")

print("\n" + "=" * 60)
print("  ✅ All version references updated to 1.0.0")
print("=" * 60)
print("\nNext steps:")
print("  1. Run prepend_changelog.py")
print("  2. cd frontend && npm run build")
print("  3. git add -A && git commit -m 'v1.0.0 — O.D.I.N. 1.0 release'")
print("  4. git tag v1.0.0")
print("  5. git push origin master --tags")
