#!/usr/bin/env python3
"""
O.D.I.N. v0.19.0 Security Fixes — Final cleanup
=================================================
Handles the remaining unprotected mutating routes.

Routes that are INTENTIONALLY unprotected:
  - /api/setup/* — guarded by _setup_is_complete() check
  - /api/auth/login — login endpoint
  - /api/jobs POST — has its own approval logic with current_user
  - /api/jobs/{id}/approve|reject|resubmit — have their own auth
  - /api/push/subscribe — user-level, needs auth but not role
  - /api/cameras/{printer_id}/webrtc — WebRTC negotiation, read-only
"""

import os, sys

def read_file(path):
    with open(path, 'r') as f:
        return f.read()

def write_file(path, content):
    with open(path, 'w') as f:
        f.write(content)

def inject_rbac(content, route_decorator, role, label):
    """Find a route decorator and add require_role to the next function def."""
    idx = content.find(route_decorator)
    if idx < 0:
        print(f"  ⚠️  SKIP {label} — route not found")
        return content
    
    # Find the function def
    func_start = content.find('\ndef ', idx)
    async_start = content.find('\nasync def ', idx)
    
    # Pick whichever comes first (and is within 200 chars)
    candidates = []
    if func_start >= 0 and func_start < idx + 300:
        candidates.append(func_start)
    if async_start >= 0 and async_start < idx + 300:
        candidates.append(async_start)
    
    if not candidates:
        print(f"  ⚠️  SKIP {label} — no function def found near route")
        return content
    
    func_start = min(candidates)
    
    # Check if already has require_role
    func_end = content.find('\n', func_start + 1)
    # Look at up to 5 lines for multi-line signatures
    sig_end = content.find('):', func_start)
    if sig_end < 0 or sig_end > func_start + 500:
        print(f"  ⚠️  SKIP {label} — can't find signature end")
        return content
    
    sig_block = content[func_start:sig_end+2]
    
    if 'require_role' in sig_block:
        print(f"  ⏭️  SKIP {label} — already has require_role")
        return content
    
    if 'db: Session = Depends(get_db)' in sig_block:
        new_block = sig_block.replace(
            'db: Session = Depends(get_db)',
            f'current_user: dict = Depends(require_role("{role}")), db: Session = Depends(get_db)'
        )
        content = content[:func_start] + new_block + content[sig_end+2:]
        print(f"  ✅ {role}: {label}")
    elif 'current_user' in sig_block:
        print(f"  ⏭️  SKIP {label} — already has current_user")
    else:
        # No db param — inject before ):
        content = content[:sig_end] + f', current_user: dict = Depends(require_role("{role}"))' + content[sig_end:]
        print(f"  ✅ {role}: {label} (injected)")
    
    return content


def main():
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend', 'main.py')
    if not os.path.exists(main_py):
        print(f"❌ {main_py} not found"); sys.exit(1)
    
    content = read_file(main_py)
    original = content
    
    print("=" * 60)
    print("Final RBAC Cleanup")
    print("=" * 60)
    
    # Remaining routes that need RBAC
    routes = [
        # Models schedule
        ('@app.post("/api/models/{model_id}/schedule"', 'operator', 'model schedule'),
        # Filaments create (the one that was missed)
        ('@app.post("/api/filaments", tags=["Filaments"])', 'operator', 'filament create'),
        # Manual slot assign
        ('@app.patch("/api/printers/{printer_id}/slots/{slot_number}/manual-assign"', 'operator', 'manual slot assign'),
        # Spool operations
        ('@app.post("/api/spools/{spool_id}/load"', 'operator', 'spool load'),
        ('@app.post("/api/spools/{spool_id}/unload"', 'operator', 'spool unload'),
        ('@app.post("/api/spools/{spool_id}/use"', 'operator', 'spool use'),
        ('@app.post("/api/spools/{spool_id}/weigh"', 'operator', 'spool weigh'),
        ('@app.post("/api/printers/{printer_id}/slots/{slot_number}/assign"', 'operator', 'slot assign'),
        ('@app.post("/api/printers/{printer_id}/slots/{slot_number}/confirm"', 'operator', 'slot confirm'),
        # Print files upload
        ('@app.post("/api/print-files/upload"', 'operator', 'print file upload'),
        # Job reorder
        ('@app.patch("/api/jobs/reorder"', 'operator', 'job reorder'),
        # Config endpoints
        ('@app.put("/api/config/quiet-hours")', 'admin', 'quiet hours config'),
        ('@app.put("/api/config/mqtt-republish")', 'admin', 'mqtt republish config'),
        ('@app.post("/api/config/mqtt-republish/test")', 'admin', 'mqtt republish test'),
        # Job failure logging
        ('@app.patch("/api/jobs/{job_id}/failure"', 'operator', 'job failure log'),
        # Branding logo delete (missed earlier)
        ('@app.delete("/api/branding/logo"', 'admin', 'branding logo delete'),
        # Language settings
        ('@app.put("/api/settings/language"', 'admin', 'language update'),
        # Delete plug config
        ('@app.delete("/api/printers/{printer_id}/plug", tags=["Smart Plug"])', 'operator', 'plug delete'),
        # Energy rate
        ('@app.put("/api/settings/energy-rate"', 'admin', 'energy rate update'),
    ]
    
    for route, role, label in routes:
        content = inject_rbac(content, route, role, label)
    
    if content != original:
        write_file(main_py, content)
        print(f"\n  📝 Written {len(content)} bytes")
    else:
        print(f"\n  ℹ️  No changes")


if __name__ == "__main__":
    main()
