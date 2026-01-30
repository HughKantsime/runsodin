#!/usr/bin/env python3
"""
MQTT Fleet Test - Compare data structures across all Bambu printers
"""
import sys
sys.path.insert(0, '/opt/printfarm-scheduler/backend')

import sqlite3
import json
import time
import crypto
from bambu_adapter import BambuPrinter

def get_printers():
    conn = sqlite3.connect('/opt/printfarm-scheduler/backend/printfarm.db')
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('''
        SELECT id, name, model, api_host, api_key 
        FROM printers 
        WHERE api_type = 'bambu' AND api_host IS NOT NULL AND api_key IS NOT NULL
    ''')
    
    printers = []
    for row in cur.fetchall():
        try:
            parts = crypto.decrypt(row['api_key']).split("|")
            if len(parts) == 2:
                printers.append({
                    'id': row['id'],
                    'name': row['name'],
                    'model': row['model'],
                    'ip_address': row['api_host'],
                    'serial_number': parts[0],
                    'access_code': parts[1]
                })
        except:
            print(f"  Warning: Could not decrypt credentials for {row['name']}")
    return printers

def extract_key_fields(data):
    p = data.get('print', {})
    return {
        'gcode_state': p.get('gcode_state'),
        'job_id': p.get('job_id'),
        'subtask_name': p.get('subtask_name'),
        'gcode_file': p.get('gcode_file'),
        'mc_percent': p.get('mc_percent'),
        'layer_num': p.get('layer_num'),
        'total_layer_num': p.get('total_layer_num'),
        'mc_remaining_time': p.get('mc_remaining_time'),
        'print_error': p.get('print_error'),
        'bed_temper': p.get('bed_temper'),
        'bed_target_temper': p.get('bed_target_temper'),
        'nozzle_temper': p.get('nozzle_temper'),
        'nozzle_target_temper': p.get('nozzle_target_temper'),
        'cooling_fan_speed': p.get('cooling_fan_speed'),
        'heatbreak_fan_speed': p.get('heatbreak_fan_speed'),
        'big_fan1_speed': p.get('big_fan1_speed'),
        'big_fan2_speed': p.get('big_fan2_speed'),
        'spd_lvl': p.get('spd_lvl'),
        'spd_mag': p.get('spd_mag'),
        'has_ams': 'ams' in p and p['ams'].get('ams') is not None,
        'ams_unit_count': len(p.get('ams', {}).get('ams', [])) if p.get('ams', {}).get('ams') else 0,
        'wifi_signal': p.get('wifi_signal'),
        'nozzle_type': p.get('nozzle_type'),
        'nozzle_diameter': p.get('nozzle_diameter'),
    }

def test_printer(printer, timeout=15):
    result = {'printer': printer, 'success': False, 'data': None, 'error': None}
    captured_data = {}
    
    def on_update(status):
        nonlocal captured_data
        captured_data = status.raw_data
    
    print(f"\n{'='*60}")
    print(f"Testing: {printer['name']} ({printer['model']}) @ {printer['ip_address']}")
    print(f"{'='*60}")
    
    try:
        p = BambuPrinter(
            ip=printer['ip_address'],
            serial=printer['serial_number'],
            access_code=printer['access_code'],
            on_status_update=on_update
        )
        
        if p.connect():
            print(f"  Connected! Waiting for data...")
            start = time.time()
            while not captured_data and (time.time() - start) < timeout:
                time.sleep(0.5)
            p.disconnect()
            
            if captured_data:
                result['success'] = True
                result['data'] = captured_data
                result['key_fields'] = extract_key_fields(captured_data)
                print(f"  Got data!")
            else:
                result['error'] = "Timeout waiting for data"
                print(f"  Timeout - no data received in {timeout}s")
        else:
            result['error'] = "Connection failed"
            print(f"  Connection failed")
    except Exception as e:
        result['error'] = str(e)
        print(f"  Error: {e}")
    
    return result

def compare_results(results):
    print(f"\n{'='*60}")
    print("COMPARISON SUMMARY")
    print(f"{'='*60}")
    
    successful = [r for r in results if r['success']]
    failed = [r for r in results if not r['success']]
    
    print(f"\nConnected: {len(successful)}/{len(results)} printers")
    
    if failed:
        print(f"\nFailed printers:")
        for r in failed:
            print(f"  - {r['printer']['name']}: {r['error']}")
    
    if not successful:
        print("\nNo successful connections to compare.")
        return
    
    print(f"\n{'Field':<25} | " + " | ".join(f"{r['printer']['name'][:10]:<12}" for r in successful))
    print("-" * (28 + 15 * len(successful)))
    
    all_fields = set()
    for r in successful:
        all_fields.update(r['key_fields'].keys())
    
    for field in sorted(all_fields):
        values = []
        for r in successful:
            val = r['key_fields'].get(field)
            if val is None:
                values.append("MISSING")
            elif isinstance(val, bool):
                values.append("Yes" if val else "No")
            elif isinstance(val, (int, float)):
                values.append(str(val)[:12])
            else:
                values.append(str(val)[:12])
        print(f"{field:<25} | " + " | ".join(f"{v:<12}" for v in values))

def main():
    print("MQTT Fleet Comparison Test")
    print("=" * 60)
    
    printers = get_printers()
    print(f"Found {len(printers)} Bambu printers in database:")
    for p in printers:
        print(f"  - {p['name']} ({p['model']}) @ {p['ip_address']}")
    
    results = []
    for printer in printers:
        result = test_printer(printer)
        results.append(result)
    
    compare_results(results)

if __name__ == '__main__':
    main()
