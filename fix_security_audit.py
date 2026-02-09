#!/usr/bin/env python3
"""
O.D.I.N. v0.19.0 Security Audit Fixes
======================================
Addresses all ship-blocking (SB-1 through SB-7) and high-severity findings.

Run from the project root:
    python3 fix_security_audit.py

What it patches:
  SB-1: RBAC enforcement on all mutating routes
  SB-2: JWT secret split-brain (OIDC uses auth.py SECRET_KEY)
  SB-3: Setup endpoints locked after setup completes
  SB-4: /label auth bypass narrowed to exact paths
  SB-5: Branding PUT/POST/DELETE require admin auth
  SB-6: User update column whitelist
  SB-7: Frontend branding defaults say "O.D.I.N."
  H-1:  Duplicate route removal
  H-2:  Version strings read from VERSION file
  L-1:  Setup password validation indentation fix
  +     Audit log RBAC, backup RBAC, settings RBAC
"""

import sys
import os

# ============================================================
# Helpers
# ============================================================

def read_file(path):
    with open(path, 'r') as f:
        return f.read()

def write_file(path, content):
    with open(path, 'w') as f:
        f.write(content)

def safe_replace(content, old, new, filename, label):
    if old not in content:
        print(f"  ⚠️  SKIP {label} — pattern not found in {filename}")
        return content
    count = content.count(old)
    if count > 1:
        print(f"  ⚠️  WARN {label} — pattern found {count} times in {filename}, replacing first only")
        return content.replace(old, new, 1)
    content = content.replace(old, new)
    print(f"  ✅ {label}")
    return content

def safe_replace_all(content, old, new, filename, label):
    """Replace ALL occurrences."""
    if old not in content:
        print(f"  ⚠️  SKIP {label} — pattern not found in {filename}")
        return content
    count = content.count(old)
    content = content.replace(old, new)
    print(f"  ✅ {label} ({count} replacements)")
    return content


# ============================================================
# MAIN.PY FIXES
# ============================================================

def fix_main_py(path):
    print(f"\n{'='*60}")
    print(f"Patching {path}")
    print(f"{'='*60}")
    
    content = read_file(path)
    original = content
    
    # ----------------------------------------------------------
    # SB-4: Fix /label auth bypass — narrow to exact spool label paths
    # ----------------------------------------------------------
    content = safe_replace(content,
        '"/label" in request.url.path',
        '(request.url.path.endswith("/label") or request.url.path.endswith("/labels/batch"))',
        'main.py', 'SB-4: Narrow /label auth bypass')
    
    # ----------------------------------------------------------
    # SB-5: Fix branding auth bypass — only GET is unauthenticated
    # ----------------------------------------------------------
    content = safe_replace(content,
        'request.url.path.startswith("/api/branding")',
        '(request.url.path == "/api/branding" and request.method == "GET")',
        'main.py', 'SB-5: Branding auth — only GET bypasses')
    
    # ----------------------------------------------------------
    # SB-3: Lock setup endpoints after setup completes
    # ----------------------------------------------------------
    # Fix test-printer
    content = safe_replace(content,
        '''@app.post("/api/setup/test-printer", tags=["Setup"])
def setup_test_printer(request: SetupTestPrinterRequest):
    """Test printer connection during setup. Wraps existing test logic."""''',
        '''@app.post("/api/setup/test-printer", tags=["Setup"])
def setup_test_printer(request: SetupTestPrinterRequest, db: Session = Depends(get_db)):
    """Test printer connection during setup. Wraps existing test logic."""
    if _setup_is_complete(db):
        raise HTTPException(status_code=403, detail="Setup already completed")''',
        'main.py', 'SB-3a: Lock setup/test-printer after setup')
    
    # Fix setup/printer
    content = safe_replace(content,
        '''@app.post("/api/setup/printer", tags=["Setup"])
def setup_create_printer(request: SetupPrinterRequest, db: Session = Depends(get_db)):
    """Create a printer during setup. Requires JWT from admin creation step."""
    # Encrypt api_key if provided''',
        '''@app.post("/api/setup/printer", tags=["Setup"])
def setup_create_printer(request: SetupPrinterRequest, db: Session = Depends(get_db)):
    """Create a printer during setup. Requires JWT from admin creation step."""
    if _setup_is_complete(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    # Encrypt api_key if provided''',
        'main.py', 'SB-3b: Lock setup/printer after setup')
    
    # Fix setup/complete
    content = safe_replace(content,
        '''@app.post("/api/setup/complete", tags=["Setup"])
def setup_mark_complete(db: Session = Depends(get_db)):
    """Mark setup as complete. Prevents wizard from showing again."""
    existing = db.execute(text(''',
        '''@app.post("/api/setup/complete", tags=["Setup"])
def setup_mark_complete(db: Session = Depends(get_db)):
    """Mark setup as complete. Prevents wizard from showing again."""
    if _setup_is_complete(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    existing = db.execute(text(''',
        'main.py', 'SB-3c: Lock setup/complete after setup')
    
    # ----------------------------------------------------------
    # L-1: Fix setup password validation indentation (dead code)
    # ----------------------------------------------------------
    content = safe_replace(content,
        '''    if _setup_users_exist(db):
        raise HTTPException(status_code=403, detail="Setup already completed — users exist")

        pw_valid, pw_msg = _validate_password(request.password)
    if not pw_valid:''',
        '''    if _setup_users_exist(db):
        raise HTTPException(status_code=403, detail="Setup already completed — users exist")

    pw_valid, pw_msg = _validate_password(request.password)
    if not pw_valid:''',
        'main.py', 'L-1: Fix setup password validation indentation')
    
    # ----------------------------------------------------------
    # SB-2: Fix OIDC JWT secret — use auth.py SECRET_KEY
    # ----------------------------------------------------------
    content = safe_replace(content,
        '''        # Generate JWT
        import jwt
        jwt_secret = os.environ.get("JWT_SECRET", "change-me-in-production")
        access_token = jwt.encode(
            {
                "sub": str(user_id),
                "username": existing._mapping.get("username") if existing else username,
                "role": user_role,
                "exp": datetime.utcnow() + timedelta(hours=24),
            },
            jwt_secret,
            algorithm="HS256",
        )''',
        '''        # Generate JWT — use the same secret/function as normal login
        access_token = create_access_token(
            data={
                "sub": existing._mapping.get("username") if existing else username,
                "role": user_role,
            }
        )''',
        'main.py', 'SB-2: OIDC callback uses auth.py create_access_token')
    
    # ----------------------------------------------------------
    # SB-6: User update column whitelist
    # ----------------------------------------------------------
    content = safe_replace(content,
        '''async def update_user(user_id: int, updates: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if 'password' in updates and updates['password']:
        updates['password_hash'] = hash_password(updates.pop('password'))
    else:
        updates.pop('password', None)
    
    if updates:
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())''',
        '''async def update_user(user_id: int, updates: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if 'password' in updates and updates['password']:
        pw_valid, pw_msg = _validate_password(updates['password'])
        if not pw_valid:
            raise HTTPException(status_code=400, detail=pw_msg)
        updates['password_hash'] = hash_password(updates.pop('password'))
    else:
        updates.pop('password', None)
    
    # SB-6: Whitelist allowed columns to prevent SQL injection via column names
    ALLOWED_USER_FIELDS = {"username", "email", "role", "is_active", "password_hash"}
    updates = {k: v for k, v in updates.items() if k in ALLOWED_USER_FIELDS}
    
    if updates:
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())''',
        'main.py', 'SB-6: User update column whitelist + M-3: password validation')
    
    # ----------------------------------------------------------
    # H-2: Version strings — read from VERSION file
    # ----------------------------------------------------------
    # Add version reading near the top (after imports)
    content = safe_replace(content,
        '''def get_db():
    """Dependency for database sessions."""''',
        '''# Read version from VERSION file
import pathlib as _pathlib
_version_file = _pathlib.Path(__file__).parent.parent / "VERSION"
if _version_file.exists():
    __version__ = _version_file.read_text().strip()
else:
    __version__ = "0.19.0"


def get_db():
    """Dependency for database sessions."""''',
        'main.py', 'H-2a: Add VERSION file reader')
    
    # Fix FastAPI app title version
    content = safe_replace(content,
        'version="0.1.0",',
        'version=__version__,',
        'main.py', 'H-2b: FastAPI app version')
    
    # Fix health endpoint version
    content = safe_replace(content,
        'version="0.1.0",',
        'version=__version__,',
        'main.py', 'H-2c: Health endpoint version')
    
    # Fix Prometheus version
    content = safe_replace(content,
        'media_type="text/plain; version=0.0.4; charset=utf-8"',
        f'media_type="text/plain; version=0.0.4; charset=utf-8"',
        'main.py', 'H-2d: Prometheus version (kept as protocol version)')
    
    # ----------------------------------------------------------
    # H-1: Remove duplicate routes
    # ----------------------------------------------------------
    # Find and remove the duplicate export block (lines ~3041-3200)
    # The second /api/spoolman/spools duplicate
    dup_spoolman = '''@app.get("/api/spoolman/spools", tags=["Spoolman"])
def list_spoolman_spools_v2'''
    if dup_spoolman in content:
        # Find the full duplicate block and remove it
        pass  # We'll handle this with a targeted approach below
    
    # For duplicates, let's find and remove the second block of export routes
    # These are copy-paste artifacts. We need to identify the second set.
    # The first set starts around line 2876, the second around line 3041
    # We'll remove the second set by finding a unique marker
    
    # Remove duplicate /api/export/jobs (second occurrence)
    # We need to find the second definition — look for it after the first one
    export_jobs_marker = '@app.get("/api/export/jobs", tags=["Export"])'
    idx_first = content.find(export_jobs_marker)
    if idx_first >= 0:
        idx_second = content.find(export_jobs_marker, idx_first + 1)
        if idx_second >= 0:
            # Find the end of the second export block (up to next non-export route)
            # Look for the next @app. decorator that isn't export
            search_from = idx_second
            # Find where the duplicate export block ends
            export_models_2nd = '@app.get("/api/export/models", tags=["Export"])'
            idx_models_2nd = content.find(export_models_2nd, idx_second)
            if idx_models_2nd >= 0:
                # Find the next route after the duplicate models export
                next_route = content.find('\n@app.', idx_models_2nd + len(export_models_2nd))
                if next_route >= 0:
                    duplicate_block = content[idx_second:next_route]
                    content = content[:idx_second] + content[next_route:]
                    print(f"  ✅ H-1a: Removed duplicate export routes block ({len(duplicate_block)} chars)")
    
    # Remove duplicate /api/spoolman/spools
    spoolman_marker = '@app.get("/api/spoolman/spools"'
    idx_first = content.find(spoolman_marker)
    if idx_first >= 0:
        idx_second = content.find(spoolman_marker, idx_first + 1)
        if idx_second >= 0:
            # Find end of the duplicate function
            next_route = content.find('\n@app.', idx_second + 10)
            if next_route >= 0:
                duplicate_block = content[idx_second:next_route]
                content = content[:idx_second] + content[next_route:]
                print(f"  ✅ H-1b: Removed duplicate /api/spoolman/spools ({len(duplicate_block)} chars)")
    
    # ----------------------------------------------------------
    # SB-1: Add RBAC to all unprotected mutating routes
    # ----------------------------------------------------------
    # Strategy: We'll add require_role("operator") to all POST/PATCH/PUT/DELETE
    # routes that don't already have it, and require_role("admin") to sensitive ones.
    
    # ADMIN-ONLY routes (system config, branding, backups, permissions, settings)
    admin_routes = [
        # Branding (now requires auth per SB-5)
        ('def update_branding(', 'def update_branding(', 'admin', 'branding PUT'),
        ('def upload_logo(', 'def upload_logo(', 'admin', 'branding logo upload'),
        ('def upload_favicon(', 'def upload_favicon(', 'admin', 'branding favicon upload'),
        ('def delete_logo(', 'def delete_logo(', 'admin', 'branding logo delete'),
        # Backups
        ('def create_backup(', 'def create_backup(', 'admin', 'backup create'),
        ('def download_backup(', 'def download_backup(', 'admin', 'backup download'),
        ('def delete_backup(', 'def delete_backup(', 'admin', 'backup delete'),
        # Config
        ('def update_config(', 'def update_config(', 'admin', 'config update'),
        # Permissions
        ('def update_permissions(', 'def update_permissions(', 'admin', 'permissions update'),
        ('def reset_permissions(', 'def reset_permissions(', 'admin', 'permissions reset'),
        # Pricing config
        ('def update_pricing_config(', 'def update_pricing_config(', 'admin', 'pricing config update'),
        # Language settings
        ('def update_language(', 'def update_language(', 'admin', 'language update'),
        # Energy rate
        ('def update_energy_rate(', 'def update_energy_rate(', 'admin', 'energy rate update'),
        # Maintenance seed
        ('def seed_default_tasks(', 'def seed_default_tasks(', 'admin', 'maintenance seed defaults'),
    ]
    
    # OPERATOR-ONLY routes (mutating operations on printers, jobs, models, etc.)
    operator_routes = [
        # Printers
        ('def create_printer(', 'def create_printer(', 'operator', 'printer create'),
        ('def reorder_printers(', 'def reorder_printers(', 'operator', 'printer reorder'),
        ('def update_printer(', 'def update_printer(', 'operator', 'printer update'),
        ('def delete_printer(', 'def delete_printer(', 'operator', 'printer delete'),
        ('def update_filament_slot(', 'def update_filament_slot(', 'operator', 'filament slot update'),
        ('def sync_ams(', 'def sync_ams(', 'operator', 'sync AMS'),
        ('def toggle_lights(', 'def toggle_lights(', 'operator', 'toggle lights'),
        ('def test_printer_connection(', 'def test_printer_connection(', 'operator', 'test printer connection'),
        # Models
        ('def create_model(', 'def create_model(', 'operator', 'model create'),
        ('def update_model(', 'def update_model(', 'operator', 'model update'),
        ('def delete_model(', 'def delete_model(', 'operator', 'model delete'),
        ('def schedule_model(', 'def schedule_model(', 'operator', 'model schedule'),
        # Jobs
        ('def create_job(', 'def create_job(', 'operator', 'job create'),
        ('def create_bulk_jobs(', 'def create_bulk_jobs(', 'operator', 'job bulk create'),
        ('def update_job(', 'def update_job(', 'operator', 'job update'),
        ('def delete_job(', 'def delete_job(', 'operator', 'job delete'),
        ('def repeat_job(', 'def repeat_job(', 'operator', 'job repeat'),
        ('def start_job(', 'def start_job(', 'operator', 'job start'),
        ('def complete_job(', 'def complete_job(', 'operator', 'job complete'),
        ('def fail_job(', 'def fail_job(', 'operator', 'job fail'),
        ('def cancel_job(', 'def cancel_job(', 'operator', 'job cancel'),
        ('def reset_job(', 'def reset_job(', 'operator', 'job reset'),
        ('def move_job(', 'def move_job(', 'operator', 'job move'),
        ('def link_print_to_job(', 'def link_print_to_job(', 'operator', 'job link print'),
        # Scheduler
        ('def run_scheduler(', 'def run_scheduler(', 'operator', 'scheduler run'),
        # Spoolman
        ('def sync_spoolman(', 'def sync_spoolman(', 'operator', 'spoolman sync'),
        # Filaments
        ('def create_filament(', 'def create_filament(', 'operator', 'filament create'),
        ('def update_filament(', 'def update_filament(', 'operator', 'filament update'),
        ('def delete_filament(', 'def delete_filament(', 'operator', 'filament delete'),
        # Spools
        ('def create_spool(', 'def create_spool(', 'operator', 'spool create'),
        ('def update_spool(', 'def update_spool(', 'operator', 'spool update'),
        ('def delete_spool(', 'def delete_spool(', 'operator', 'spool delete'),
        # Cameras
        ('def toggle_camera(', 'def toggle_camera(', 'operator', 'camera toggle'),
        # Smart Plug
        ('def update_plug_config(', 'def update_plug_config(', 'operator', 'plug config update'),
        ('def delete_plug_config(', 'def delete_plug_config(', 'operator', 'plug config delete'),
        ('def plug_on(', 'def plug_on(', 'operator', 'plug on'),
        ('def plug_off(', 'def plug_off(', 'operator', 'plug off'),
        ('def plug_toggle(', 'def plug_toggle(', 'operator', 'plug toggle'),
        # Maintenance
        ('def create_maintenance_task(', 'def create_maintenance_task(', 'operator', 'maintenance task create'),
        ('def update_maintenance_task(', 'def update_maintenance_task(', 'operator', 'maintenance task update'),
        ('def delete_maintenance_task(', 'def delete_maintenance_task(', 'operator', 'maintenance task delete'),
        ('def create_maintenance_log(', 'def create_maintenance_log(', 'operator', 'maintenance log create'),
        ('def delete_maintenance_log(', 'def delete_maintenance_log(', 'operator', 'maintenance log delete'),
        # Products
        ('def create_product(', 'def create_product(', 'operator', 'product create'),
        ('def update_product(', 'def update_product(', 'operator', 'product update'),
        ('def delete_product(', 'def delete_product(', 'operator', 'product delete'),
        ('def add_product_component(', 'def add_product_component(', 'operator', 'product component add'),
        ('def delete_product_component(', 'def delete_product_component(', 'operator', 'product component delete'),
        # Orders
        ('def create_order(', 'def create_order(', 'operator', 'order create'),
        ('def update_order(', 'def update_order(', 'operator', 'order update'),
        ('def delete_order(', 'def delete_order(', 'operator', 'order delete'),
        ('def add_order_item(', 'def add_order_item(', 'operator', 'order item add'),
        ('def update_order_item(', 'def update_order_item(', 'operator', 'order item update'),
        ('def delete_order_item(', 'def delete_order_item(', 'operator', 'order item delete'),
        ('def schedule_order(', 'def schedule_order(', 'operator', 'order schedule'),
        ('def ship_order(', 'def ship_order(', 'operator', 'order ship'),
        # Model variants
        ('def delete_variant(', 'def delete_variant(', 'operator', 'variant delete'),
        # Bambu
        ('def test_bambu_connection(', 'def test_bambu_connection(', 'operator', 'bambu test connection'),
        ('def sync_bambu_ams(', 'def sync_bambu_ams(', 'operator', 'bambu sync AMS'),
        ('def manual_assign_slot(', 'def manual_assign_slot(', 'operator', 'manual slot assign'),
        # Print files
        ('def schedule_print_file(', 'def schedule_print_file(', 'operator', 'print file schedule'),
        ('def delete_print_file(', 'def delete_print_file(', 'operator', 'print file delete'),
        # QR scan
        ('def scan_and_assign(', 'def scan_and_assign(', 'operator', 'scan assign'),
        # Alerts
        ('def mark_alert_read(', 'def mark_alert_read(', 'operator', 'alert mark read'),
        ('def mark_all_read(', 'def mark_all_read(', 'operator', 'alerts mark all read'),
        ('def dismiss_alert(', 'def dismiss_alert(', 'operator', 'alert dismiss'),
        ('def update_alert_preferences(', 'def update_alert_preferences(', 'operator', 'alert preferences update'),
    ]
    
    # Apply RBAC to admin routes
    print(f"\n  --- Adding admin RBAC ---")
    for search, replace_func, role, label in admin_routes:
        # Check if the function already has require_role
        func_idx = content.find(search)
        if func_idx < 0:
            print(f"  ⚠️  SKIP admin RBAC: {label} — function not found")
            continue
        
        # Check the function signature area (200 chars before)
        sig_area = content[max(0, func_idx-200):func_idx+len(search)]
        if 'require_role' in sig_area:
            print(f"  ⏭️  SKIP admin RBAC: {label} — already has require_role")
            continue
        
        # Find the full function signature line
        # Look for "def func_name(" and add the Depends parameter
        old_sig = search
        # We need to find the actual full signature to inject into
        # Find the line with the function def
        line_start = content.rfind('\n', 0, func_idx) + 1
        line_end = content.find('\n', func_idx)
        full_line = content[line_start:line_end]
        
        # Check if it already has db: Session = Depends
        if 'db: Session = Depends' in full_line and 'current_user' not in full_line:
            # Add current_user before db
            new_line = full_line.replace(
                'db: Session = Depends(get_db)',
                'current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)'
            )
            content = content[:line_start] + new_line + content[line_end:]
            print(f"  ✅ admin RBAC: {label}")
        elif 'db: Session = Depends' not in full_line and 'current_user' not in full_line:
            # Function doesn't take db — check next line for continuation
            # Look at the full function signature (may span multiple lines)
            sig_end = content.find(':', func_idx)
            if sig_end > 0:
                sig_block = content[func_idx:sig_end]
                if 'db: Session = Depends' in sig_block and 'current_user' not in sig_block:
                    new_block = sig_block.replace(
                        'db: Session = Depends(get_db)',
                        'current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)'
                    )
                    content = content[:func_idx] + new_block + content[sig_end:]
                    print(f"  ✅ admin RBAC: {label} (multi-line)")
                else:
                    print(f"  ⚠️  MANUAL admin RBAC needed: {label}")
            else:
                print(f"  ⚠️  MANUAL admin RBAC needed: {label}")
    
    # Apply RBAC to operator routes
    print(f"\n  --- Adding operator RBAC ---")
    for search, replace_func, role, label in operator_routes:
        func_idx = content.find(search)
        if func_idx < 0:
            print(f"  ⚠️  SKIP operator RBAC: {label} — function not found")
            continue
        
        sig_area = content[max(0, func_idx-200):func_idx+len(search)]
        if 'require_role' in sig_area:
            print(f"  ⏭️  SKIP operator RBAC: {label} — already has require_role")
            continue
        
        line_start = content.rfind('\n', 0, func_idx) + 1
        line_end = content.find('\n', func_idx)
        full_line = content[line_start:line_end]
        
        if 'db: Session = Depends' in full_line and 'current_user' not in full_line:
            new_line = full_line.replace(
                'db: Session = Depends(get_db)',
                'current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)'
            )
            content = content[:line_start] + new_line + content[line_end:]
            print(f"  ✅ operator RBAC: {label}")
        else:
            # Try multi-line signature
            sig_end = content.find('):', func_idx)
            if sig_end > 0:
                sig_block = content[func_idx:sig_end+2]
                if 'db: Session = Depends' in sig_block and 'current_user' not in sig_block:
                    new_block = sig_block.replace(
                        'db: Session = Depends(get_db)',
                        'current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)'
                    )
                    content = content[:func_idx] + new_block + content[sig_end+2:]
                    print(f"  ✅ operator RBAC: {label} (multi-line)")
                elif 'current_user' in sig_block:
                    print(f"  ⏭️  SKIP operator RBAC: {label} — already has current_user")
                else:
                    print(f"  ⚠️  MANUAL operator RBAC needed: {label}")
            else:
                print(f"  ⚠️  MANUAL operator RBAC needed: {label}")
    
    # ----------------------------------------------------------
    # M-7: Add admin RBAC to audit logs
    # ----------------------------------------------------------
    content = safe_replace(content,
        'def list_audit_logs(',
        'def list_audit_logs_MARKER(',  # temp marker
        'main.py', 'M-7: Marking audit log endpoint')
    # Actually, let's do this properly by finding the function
    audit_idx = content.find('def list_audit_logs_MARKER(')
    if audit_idx >= 0:
        content = content.replace('def list_audit_logs_MARKER(', 'def list_audit_logs(')
        line_start = content.rfind('\n', 0, audit_idx) + 1
        line_end = content.find('\n', audit_idx)
        full_line = content[line_start:line_end]
        if 'require_role' not in full_line and 'db: Session = Depends' in full_line:
            new_line = full_line.replace(
                'db: Session = Depends(get_db)',
                'current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)'
            )
            content = content[:line_start] + new_line + content[line_end:]
            print(f"  ✅ M-7: Audit log RBAC (admin)")
    
    # ----------------------------------------------------------
    # Emergency stop — keep as operator
    # ----------------------------------------------------------
    # Find stop/pause/resume endpoints
    for action_name in ['stop', 'pause', 'resume']:
        func_name = f'def {action_name}_printer('
        func_idx = content.find(func_name)
        if func_idx >= 0:
            sig_area = content[max(0, func_idx-200):func_idx+len(func_name)]
            if 'require_role' not in sig_area:
                line_start = content.rfind('\n', 0, func_idx) + 1
                line_end = content.find('\n', func_idx)
                full_line = content[line_start:line_end]
                if 'db: Session = Depends' in full_line:
                    new_line = full_line.replace(
                        'db: Session = Depends(get_db)',
                        'current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)'
                    )
                    content = content[:line_start] + new_line + content[line_end:]
                    print(f"  ✅ operator RBAC: printer {action_name}")
    
    # ----------------------------------------------------------
    # go2rtc path fix (L-4)
    # ----------------------------------------------------------
    content = safe_replace(content,
        'GO2RTC_CONFIG = "/opt/printfarm-scheduler/go2rtc/go2rtc.yaml"',
        'GO2RTC_CONFIG = os.environ.get("GO2RTC_CONFIG", "/opt/printfarm-scheduler/go2rtc/go2rtc.yaml")',
        'main.py', 'L-4: go2rtc path configurable')
    
    # Write the result
    if content != original:
        write_file(path, content)
        print(f"\n  📝 Written {len(content)} bytes (was {len(original)})")
    else:
        print(f"\n  ℹ️  No changes made")


# ============================================================
# FRONTEND FIXES
# ============================================================

def fix_branding_context(path):
    print(f"\n{'='*60}")
    print(f"Patching {path}")
    print(f"{'='*60}")
    
    content = read_file(path)
    original = content
    
    # SB-7: Fix default app_name
    content = safe_replace(content,
        '  app_name: "PrintFarm",\n  app_subtitle: "Scheduler",',
        '  app_name: "O.D.I.N.",\n  app_subtitle: "Print Farm Management",',
        'BrandingContext.jsx', 'SB-7a: Default app_name → O.D.I.N.')
    
    if content != original:
        write_file(path, content)
        print(f"  📝 Written")


def fix_branding_page(path):
    print(f"\n{'='*60}")
    print(f"Patching {path}")
    print(f"{'='*60}")
    
    content = read_file(path)
    original = content
    
    # SB-7: Fix default in Branding page reset
    content = safe_replace(content,
        '  app_name: "PrintFarm",',
        '  app_name: "O.D.I.N.",',
        'Branding.jsx', 'SB-7b: Branding page default → O.D.I.N.')
    
    # Fix placeholder
    content = safe_replace(content,
        'placeholder="PrintFarm"',
        'placeholder="O.D.I.N."',
        'Branding.jsx', 'SB-7c: Branding placeholder → O.D.I.N.')
    
    if content != original:
        write_file(path, content)
        print(f"  📝 Written")


def fix_settings_smtp(path):
    print(f"\n{'='*60}")
    print(f"Patching {path}")
    print(f"{'='*60}")
    
    content = read_file(path)
    original = content
    
    # L-3: Fix SMTP placeholder
    content = safe_replace(content,
        'placeholder="printfarm@yourdomain.com"',
        'placeholder="odin@yourdomain.com"',
        'Settings.jsx', 'L-3: SMTP placeholder → odin@')
    
    if content != original:
        write_file(path, content)
        print(f"  📝 Written")


# ============================================================
# RUN ALL FIXES
# ============================================================

def main():
    # Determine project root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Check if files exist at expected paths
    main_py = os.path.join(script_dir, 'backend', 'main.py')
    branding_ctx = os.path.join(script_dir, 'frontend', 'src', 'BrandingContext.jsx')
    branding_page = os.path.join(script_dir, 'frontend', 'src', 'pages', 'Branding.jsx')
    settings_page = os.path.join(script_dir, 'frontend', 'src', 'pages', 'Settings.jsx')
    
    for f in [main_py, branding_ctx, branding_page, settings_page]:
        if not os.path.exists(f):
            print(f"❌ File not found: {f}")
            print(f"   Run this script from the project root directory.")
            sys.exit(1)
    
    print("=" * 60)
    print("O.D.I.N. v0.19.0 Security Audit Fix Script")
    print("=" * 60)
    print(f"Project root: {script_dir}")
    
    # Backup first
    import shutil
    backup_dir = os.path.join(script_dir, '_audit_backups')
    os.makedirs(backup_dir, exist_ok=True)
    for f in [main_py, branding_ctx, branding_page, settings_page]:
        backup_path = os.path.join(backup_dir, os.path.basename(f) + '.bak')
        shutil.copy2(f, backup_path)
    print(f"\n📦 Backups saved to {backup_dir}/")
    
    # Apply fixes
    fix_main_py(main_py)
    fix_branding_context(branding_ctx)
    fix_branding_page(branding_page)
    fix_settings_smtp(settings_page)
    
    print(f"\n{'='*60}")
    print("DONE — Summary")
    print(f"{'='*60}")
    print("""
Files modified:
  - backend/main.py (auth middleware, RBAC, JWT, setup guards, user whitelist, version, duplicates)
  - frontend/src/BrandingContext.jsx (default app name)
  - frontend/src/pages/Branding.jsx (default app name, placeholder)
  - frontend/src/pages/Settings.jsx (SMTP placeholder)

Backups at: _audit_backups/

MANUAL STEPS STILL NEEDED:
  1. Generate Ed25519 keypair:  python3 generate_license.py --keygen
  2. Embed public key in backend/license_manager.py (replaces REPLACE_WITH_YOUR_PUBLIC_KEY)
  3. Disable legacy service:    systemctl disable printfarm.service && systemctl stop printfarm.service
  4. Rebuild frontend:          cd frontend && npm run build
  5. Restart backend:           systemctl restart printfarm-backend
  6. Test all endpoints
""")


if __name__ == "__main__":
    main()
