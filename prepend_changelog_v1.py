#!/usr/bin/env python3
"""Prepend v1.0.0 changelog entry"""
import datetime

CHANGELOG = "/opt/printfarm-scheduler/CHANGELOG.md"

entry = f"""
## [1.0.0] — {datetime.date.today().isoformat()}

### 🎉 O.D.I.N. 1.0 — First Public Release

**Security Audit & Hardening (53 findings resolved):**
- RBAC enforcement on all mutating API routes (SB-1)
- JWT secret unification — OIDC and local auth use same signing key (SB-2)
- Setup endpoints locked after initial setup completes (SB-3)
- Auth bypass fixes: `/label` path matching, branding endpoint protection (SB-4, SB-5)
- User update column whitelist prevents SQL injection via column names (SB-6)
- Frontend branding defaults corrected to O.D.I.N. (SB-7)
- Duplicate route definitions removed (H-1)
- Version string consistency across all endpoints (H-2)
- Password validation enforced on user updates (M-3)
- Admin RBAC on audit logs and backup downloads (M-7, M-11)
- Setup password validation indentation fix (L-1)

**License System (Production-Ready):**
- Ed25519 keypair generated and embedded
- Dev bypass removed — all licenses cryptographically verified
- License generation and verification workflow validated end-to-end
- Founders Program ready (90-day Pro keys via Discord)

**Version Bump:**
- 0.19.x → 1.0.0 across all files (VERSION, main.py, package.json, Prometheus, health endpoint)
- Git tag: v1.0.0

"""

with open(CHANGELOG, 'r') as f:
    existing = f.read()

# Insert after first line (the # header)
lines = existing.split('\n', 1)
if len(lines) == 2:
    new_content = lines[0] + '\n' + entry + lines[1]
else:
    new_content = lines[0] + '\n' + entry

with open(CHANGELOG, 'w') as f:
    f.write(new_content)

print(f"✅ CHANGELOG.md updated with v1.0.0 entry")
