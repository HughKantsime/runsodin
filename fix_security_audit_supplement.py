#!/usr/bin/env python3
"""
O.D.I.N. v0.19.0 Security Audit Fixes — Supplemental
=====================================================
Patches routes that the main script couldn't auto-find due to
different function names.

Run AFTER fix_security_audit.py:
    python3 fix_security_audit_supplement.py
"""

import os
import sys

def read_file(path):
    with open(path, 'r') as f:
        return f.read()

def write_file(path, content):
    with open(path, 'w') as f:
        f.write(content)

def safe_replace(content, old, new, label):
    if old not in content:
        print(f"  ⚠️  SKIP {label} — pattern not found")
        return content
    count = content.count(old)
    if count > 1:
        print(f"  ⚠️  WARN {label} — found {count} times, replacing first")
        return content.replace(old, new, 1)
    content = content.replace(old, new)
    print(f"  ✅ {label}")
    return content


def main():
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend', 'main.py')
    if not os.path.exists(main_py):
        print(f"❌ {main_py} not found")
        sys.exit(1)
    
    content = read_file(main_py)
    original = content
    
    print("=" * 60)
    print("Supplemental RBAC Fixes")
    print("=" * 60)
    
    # --- OPERATOR routes ---
    
    # sync_ams_state
    content = safe_replace(content,
        'def sync_ams_state(printer_id: int, db: Session = Depends(get_db)):',
        'def sync_ams_state(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):',
        'operator: sync_ams_state')
    
    # toggle_printer_lights
    content = safe_replace(content,
        'def toggle_printer_lights(printer_id: int, db: Session = Depends(get_db)):',
        'def toggle_printer_lights(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):',
        'operator: toggle_printer_lights')
    
    # create_jobs_bulk
    content = safe_replace(content,
        'def create_jobs_bulk(jobs: List[JobCreate], db: Session = Depends(get_db)):',
        'def create_jobs_bulk(jobs: List[JobCreate], current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):',
        'operator: create_jobs_bulk')
    
    # link_job_to_print
    content = safe_replace(content,
        'def link_job_to_print(job_id: int, print_job_id: int, db: Session = Depends(get_db)):',
        'def link_job_to_print(job_id: int, print_job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):',
        'operator: link_job_to_print')
    
    # delete_model_variant
    content = safe_replace(content,
        'def delete_model_variant(model_id: int, variant_id: int, db: Session = Depends(get_db)):',
        'def delete_model_variant(model_id: int, variant_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):',
        'operator: delete_model_variant')
    
    # remove_product_component
    content = safe_replace(content,
        'def remove_product_component(product_id: int, component_id: int, db: Session = Depends(get_db)):',
        'def remove_product_component(product_id: int, component_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):',
        'operator: remove_product_component')
    
    # scan_assign_spool — check its exact signature
    content = safe_replace(content,
        'def scan_assign_spool(\n',
        'def scan_assign_spool_MARKER(\n',
        'operator: scan_assign_spool (marking)')
    # Find and fix it
    marker_idx = content.find('def scan_assign_spool_MARKER(')
    if marker_idx >= 0:
        content = content.replace('def scan_assign_spool_MARKER(', 'def scan_assign_spool(')
        sig_end = content.find('):', marker_idx)
        if sig_end >= 0:
            sig_block = content[marker_idx:sig_end+2]
            if 'require_role' not in sig_block and 'db: Session = Depends' in sig_block:
                new_block = sig_block.replace(
                    'db: Session = Depends(get_db)',
                    'current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)'
                )
                content = content[:marker_idx] + new_block + content[sig_end+2:]
                print(f"  ✅ operator: scan_assign_spool")
    
    # --- ADMIN routes ---
    
    # update_config — has no db param, just config: ConfigUpdate
    content = safe_replace(content,
        'def update_config(config: ConfigUpdate):',
        'def update_config(config: ConfigUpdate, current_user: dict = Depends(require_role("admin"))):',
        'admin: update_config')
    
    # download_backup — has no db param
    content = safe_replace(content,
        'def download_backup(filename: str):',
        'def download_backup(filename: str, current_user: dict = Depends(require_role("admin"))):',
        'admin: download_backup')
    
    # test_bambu_printer_connection
    content = safe_replace(content,
        'async def test_bambu_printer_connection(request: BambuConnectionTest):',
        'async def test_bambu_printer_connection(request: BambuConnectionTest, current_user: dict = Depends(require_role("operator"))):',
        'operator: test_bambu_printer_connection')
    
    # seed_default_maintenance_tasks
    content = safe_replace(content,
        'def seed_default_maintenance_tasks(db: Session = Depends(get_db)):',
        'def seed_default_maintenance_tasks(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):',
        'admin: seed_default_maintenance_tasks')
    
    # update_pricing_config — check its signature
    content = safe_replace(content,
        'def update_pricing_config(\n',
        'def update_pricing_config_MARKER(\n',
        'admin: update_pricing_config (marking)')
    marker_idx = content.find('def update_pricing_config_MARKER(')
    if marker_idx >= 0:
        content = content.replace('def update_pricing_config_MARKER(', 'def update_pricing_config(')
        sig_end = content.find('):', marker_idx)
        if sig_end >= 0:
            sig_block = content[marker_idx:sig_end+2]
            if 'require_role' not in sig_block and 'db: Session = Depends' in sig_block:
                new_block = sig_block.replace(
                    'db: Session = Depends(get_db)',
                    'current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)'
                )
                content = content[:marker_idx] + new_block + content[sig_end+2:]
                print(f"  ✅ admin: update_pricing_config")
    
    # run_scheduler_endpoint
    content = safe_replace(content,
        'def run_scheduler_endpoint(\n',
        'def run_scheduler_endpoint_MARKER(\n',
        'operator: run_scheduler (marking)')
    marker_idx = content.find('def run_scheduler_endpoint_MARKER(')
    if marker_idx >= 0:
        content = content.replace('def run_scheduler_endpoint_MARKER(', 'def run_scheduler_endpoint(')
        sig_end = content.find('):', marker_idx)
        if sig_end >= 0:
            sig_block = content[marker_idx:sig_end+2]
            if 'require_role' not in sig_block and 'db: Session = Depends' in sig_block:
                new_block = sig_block.replace(
                    'db: Session = Depends(get_db)',
                    'current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)'
                )
                content = content[:marker_idx] + new_block + content[sig_end+2:]
                print(f"  ✅ operator: run_scheduler_endpoint")
    
    # Smart plug endpoints — find by route decorator patterns
    plug_routes = [
        ('/api/printers/{printer_id}/plug/on"', 'operator', 'plug on'),
        ('/api/printers/{printer_id}/plug/off"', 'operator', 'plug off'),
        ('/api/printers/{printer_id}/plug/toggle"', 'operator', 'plug toggle'),
    ]
    for route_path, role, label in plug_routes:
        route_idx = content.find(route_path)
        if route_idx < 0:
            print(f"  ⚠️  SKIP {label} — route not found")
            continue
        # Find the function def on the next line
        func_line_start = content.find('\ndef ', route_idx)
        if func_line_start < 0 or func_line_start > route_idx + 200:
            # Try async def
            func_line_start = content.find('\nasync def ', route_idx)
        if func_line_start >= 0 and func_line_start < route_idx + 200:
            func_line_end = content.find('\n', func_line_start + 1)
            func_line = content[func_line_start+1:func_line_end]
            if 'require_role' not in func_line:
                if 'db: Session = Depends(get_db)' in func_line:
                    new_line = func_line.replace(
                        'db: Session = Depends(get_db)',
                        f'current_user: dict = Depends(require_role("{role}")), db: Session = Depends(get_db)'
                    )
                    content = content[:func_line_start+1] + new_line + content[func_line_end:]
                    print(f"  ✅ {role}: {label}")
                elif '):\n' in content[func_line_start:func_line_start+200]:
                    # Simple function with no db param — add it
                    old_sig_end = content.find('):', func_line_start)
                    if old_sig_end >= 0:
                        # Insert before the closing ):
                        insert_point = old_sig_end
                        existing_params = content[func_line_start:old_sig_end]
                        if existing_params.strip().endswith('('):
                            content = content[:old_sig_end] + f'current_user: dict = Depends(require_role("{role}"))' + content[old_sig_end:]
                        else:
                            content = content[:old_sig_end] + f', current_user: dict = Depends(require_role("{role}"))' + content[old_sig_end:]
                        print(f"  ✅ {role}: {label} (injected)")
                else:
                    print(f"  ⚠️  MANUAL: {label}")
            else:
                print(f"  ⏭️  {label} — already has require_role")
    
    # delete plug config
    route_idx = content.find('"/api/printers/{printer_id}/plug", tags=["Smart Plug"])\n')
    if route_idx >= 0:
        # There are GET, PUT, DELETE for /plug — find the DELETE one
        delete_idx = content.rfind('@app.delete(', 0, route_idx + 100)
        if delete_idx < 0:
            # Search forward from last PUT
            pass
    # Try to find by the function pattern in delete context
    delete_plug_pattern = '@app.delete("/api/printers/{printer_id}/plug"'
    dpi = content.find(delete_plug_pattern)
    if dpi >= 0:
        func_start = content.find('\ndef ', dpi)
        if func_start >= 0 and func_start < dpi + 200:
            func_end = content.find('\n', func_start + 1)
            func_line = content[func_start+1:func_end]
            if 'require_role' not in func_line and 'db: Session = Depends' in func_line:
                new_line = func_line.replace(
                    'db: Session = Depends(get_db)',
                    'current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)'
                )
                content = content[:func_start+1] + new_line + content[func_end:]
                print(f"  ✅ operator: plug config delete")
    
    # Delete order item
    doi_pattern = '"/api/orders/{order_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT'
    doi_idx = content.find(doi_pattern)
    if doi_idx >= 0:
        func_start = content.find('\ndef ', doi_idx)
        if func_start >= 0 and func_start < doi_idx + 200:
            func_end = content.find('\n', func_start + 1)
            func_line = content[func_start+1:func_end]
            if 'require_role' not in func_line and 'db: Session = Depends' in func_line:
                new_line = func_line.replace(
                    'db: Session = Depends(get_db)',
                    'current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)'
                )
                content = content[:func_start+1] + new_line + content[func_end:]
                print(f"  ✅ operator: delete order item")
    
    # test_printer_connection (main, not bambu or setup)
    tpc_pattern = '@app.post("/api/printers/test-connection", tags=["Printers"])'
    tpc_idx = content.find(tpc_pattern)
    if tpc_idx >= 0:
        func_start = content.find('\ndef ', tpc_idx)
        if func_start >= 0 and func_start < tpc_idx + 200:
            func_end = content.find('\n', func_start + 1)
            func_line = content[func_start+1:func_end]
            if 'require_role' not in func_line:
                # This one may not have db
                if 'db: Session = Depends' in func_line:
                    new_line = func_line.replace(
                        'db: Session = Depends(get_db)',
                        'current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)'
                    )
                else:
                    # Find the closing ): and inject
                    sig_end = content.find('):', func_start)
                    if sig_end >= 0 and sig_end < func_start + 300:
                        content = content[:sig_end] + ', current_user: dict = Depends(require_role("operator"))' + content[sig_end:]
                        print(f"  ✅ operator: test_printer_connection")
                        func_line = None  # skip the replace below
                if func_line:
                    content = content[:func_start+1] + new_line + content[func_end:]
                    print(f"  ✅ operator: test_printer_connection")
    
    # Write
    if content != original:
        write_file(main_py, content)
        print(f"\n  📝 Written {len(content)} bytes (was {len(original)})")
    else:
        print(f"\n  ℹ️  No changes made")


if __name__ == "__main__":
    main()
