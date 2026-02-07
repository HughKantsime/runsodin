"""
Migration: Configure Kobra S1 for Moonraker + patch monitor daemon

Run on server:
    cd /opt/printfarm-scheduler/backend
    source venv/bin/activate
    python3 migrate_moonraker.py

What this does:
1. Updates printer #5 (Kobra S1) with Moonraker connection info
2. Patches mqtt_monitor.py to also load and poll Moonraker printers
"""

import sqlite3
import os
import sys

DB_PATH = "/opt/printfarm-scheduler/backend/printfarm.db"
MONITOR_PATH = "/opt/printfarm-scheduler/backend/mqtt_monitor.py"

KOBRA_IP = "YOUR_PRINTER_IP"
KOBRA_PORT = 80


def update_printer():
    """Set Kobra S1 (printer #5) to use Moonraker."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Check current state
    cur.execute("SELECT id, name, api_type, api_host FROM printers WHERE id = 5")
    row = cur.fetchone()
    if not row:
        print("ERROR: Printer #5 not found in database")
        conn.close()
        return False
    
    print(f"Current: id={row[0]}, name={row[1]}, api_type={row[2]}, api_host={row[3]}")
    
    # Update to moonraker
    cur.execute("""
        UPDATE printers 
        SET api_type = 'moonraker',
            api_host = ?,
            model = 'Anycubic Kobra S1 (Rinkhals)',
            is_active = 1
        WHERE id = 5
    """, (f"{KOBRA_IP}:{KOBRA_PORT}",))
    
    conn.commit()
    conn.close()
    print(f"Updated printer #5: api_type=moonraker, api_host={KOBRA_IP}:{KOBRA_PORT}")
    return True


def patch_monitor():
    """
    Patch mqtt_monitor.py to load Moonraker printers alongside Bambu.
    
    Adds:
    - import for MoonrakerMonitor
    - load_moonraker_printers() method
    - Moonraker monitors started in start()
    """
    if not os.path.exists(MONITOR_PATH):
        print(f"ERROR: {MONITOR_PATH} not found")
        return False
    
    with open(MONITOR_PATH, "r") as f:
        content = f.read()
    
    # Check if already patched
    if "MoonrakerMonitor" in content:
        print("Monitor already patched for Moonraker — skipping")
        return True
    
    # === Patch 1: Add import near the top ===
    # Find the existing bambu import line
    old_import = "from bambu_adapter import BambuPrinter"
    new_import = """from bambu_adapter import BambuPrinter

# Moonraker support (Klipper printers like Kobra S1 w/ Rinkhals)
try:
    from moonraker_monitor import MoonrakerMonitor
    MOONRAKER_AVAILABLE = True
except ImportError:
    MOONRAKER_AVAILABLE = False
    log.warning("moonraker_monitor not found — Moonraker printers will be skipped")"""
    
    if old_import not in content:
        print("ERROR: Could not find bambu_adapter import line to patch")
        return False
    
    content = content.replace(old_import, new_import, 1)
    
    # === Patch 2: Add load_moonraker_printers method ===
    # Find the load_printers method's closing and add after it
    old_load_end = "        conn.close()\n        return printers"
    new_load_end = """        conn.close()
        return printers

    def load_moonraker_printers(self):
        \"\"\"Load Moonraker-based printers from database.\"\"\"
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(\"\"\"
            SELECT id, name, api_host
            FROM printers
            WHERE api_type = 'moonraker'
              AND api_host IS NOT NULL
              AND api_host != ''
              AND is_active = 1
        \"\"\")

        printers = []
        for row in cur.fetchall():
            host_str = row['api_host']
            # Parse host:port
            if ':' in host_str:
                host, port_str = host_str.rsplit(':', 1)
                try:
                    port = int(port_str)
                except ValueError:
                    host = host_str
                    port = 80
            else:
                host = host_str
                port = 80

            printers.append({
                'id': row['id'],
                'name': row['name'],
                'host': host,
                'port': port,
            })

        conn.close()
        return printers"""
    
    if old_load_end not in content:
        print("ERROR: Could not find load_printers return block to patch")
        return False
    
    content = content.replace(old_load_end, new_load_end, 1)
    
    # === Patch 3: Start Moonraker monitors in start() ===
    # Find where Bambu monitors finish connecting and add Moonraker startup
    old_connected_log = '        log.info(f"Connected to {len(self.monitors)}/{len(printers)} printers")'
    new_connected_log = """        log.info(f"Connected to {len(self.monitors)}/{len(printers)} Bambu printers")

        # Start Moonraker monitors
        if MOONRAKER_AVAILABLE:
            mk_printers = self.load_moonraker_printers()
            if mk_printers:
                log.info(f"Starting {len(mk_printers)} Moonraker monitor(s)")
                for p in mk_printers:
                    monitor = MoonrakerMonitor(
                        printer_id=p['id'],
                        name=p['name'],
                        host=p['host'],
                        port=p['port'],
                    )
                    if monitor.connect():
                        self.monitors[p['id']] = monitor
                log.info(f"Moonraker monitors connected")
            else:
                log.info("No Moonraker printers configured")"""
    
    if old_connected_log not in content:
        # Try without the f-string exact match — might have slight variations
        # Fall back to a simpler pattern
        print("WARNING: Could not find exact connected log line.")
        print("You may need to manually add Moonraker startup to the start() method.")
        print("See the patch instructions below.")
    else:
        content = content.replace(old_connected_log, new_connected_log, 1)
    
    # Write patched file
    with open(MONITOR_PATH, "w") as f:
        f.write(content)
    
    print(f"Patched {MONITOR_PATH} with Moonraker support")
    return True


def main():
    print("=" * 50)
    print("Moonraker Integration Migration")
    print("=" * 50)
    
    print("\n--- Step 1: Update Kobra S1 printer record ---")
    if not update_printer():
        print("FAILED — printer update aborted")
        sys.exit(1)
    
    print("\n--- Step 2: Patch mqtt_monitor.py ---")
    if not patch_monitor():
        print("FAILED — monitor patch aborted")
        sys.exit(1)
    
    print("\n" + "=" * 50)
    print("Migration complete!")
    print()
    print("Next steps:")
    print("  1. Copy moonraker_adapter.py and moonraker_monitor.py to backend/")
    print("  2. Restart the monitor service:")
    print("     systemctl restart printfarm-monitor")
    print("  3. Check logs:")
    print("     journalctl -u printfarm-monitor -f")
    print()
    print("Expected output:")
    print("  [INFO] Starting monitor for 4 printers")
    print("  [INFO] Connected to 4/4 Bambu printers")
    print("  [INFO] Starting 1 Moonraker monitor(s)")
    print(f"  [INFO] Connected to Anycubic Kobra S1 (Rinkhals) at {KOBRA_IP}")
    print("  [INFO] Moonraker monitors connected")
    print("=" * 50)


if __name__ == "__main__":
    main()
