"""
Hotfix: Fix alert enum case mismatch and nullable boolean handling.

Issues:
1. SQLAlchemy SQLEnum stores enum NAMES (PRINT_COMPLETE) not values (print_complete)
2. mqtt_monitor raw SQL writes lowercase values
3. is_read/is_dismissed can be NULL from raw SQL inserts

Fixes:
1. schemas.py: Make is_read/is_dismissed Optional with default False
2. mqtt_monitor.py: Write uppercase enum names in _dispatch_alert
3. Clean up any existing bad data in DB
"""
import os

BASE = "/opt/printfarm-scheduler/backend"


def fix_schemas():
    path = os.path.join(BASE, "schemas.py")
    with open(path, "r") as f:
        content = f.read()

    content = content.replace(
        "    is_read: bool = False\n    is_dismissed: bool = False",
        "    is_read: Optional[bool] = False\n    is_dismissed: Optional[bool] = False"
    )
    with open(path, "w") as f:
        f.write(content)
    print("  schemas.py: Made is_read/is_dismissed Optional")


def fix_mqtt_monitor():
    path = os.path.join(BASE, "mqtt_monitor.py")
    with open(path, "r") as f:
        content = f.read()

    # Fix _dispatch_alert to write uppercase enum names
    content = content.replace(
        """            cur.execute(\"\"\"
                    INSERT INTO alerts 
                    (user_id, alert_type, severity, title, message, 
                     printer_id, job_id, spool_id, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                \"\"\", (user_id, alert_type, severity, title, message,""",
        """            cur.execute(\"\"\"
                    INSERT INTO alerts 
                    (user_id, alert_type, severity, title, message, 
                     printer_id, job_id, spool_id, metadata_json, is_read, is_dismissed, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
                \"\"\", (user_id, alert_type.upper(), severity.upper(), title, message,"""
    )

    # Also fix the dedup query to use uppercase
    content = content.replace(
        "AND alert_type = 'spool_low'",
        "AND alert_type = 'SPOOL_LOW'"
    )

    with open(path, "w") as f:
        f.write(content)
    print("  mqtt_monitor.py: Fixed enum case to uppercase + explicit booleans")


def fix_existing_data():
    import sqlite3
    conn = sqlite3.connect(os.path.join(BASE, "printfarm.db"))
    cur = conn.cursor()

    # Fix any lowercase enum values
    mappings = {
        'print_complete': 'PRINT_COMPLETE',
        'print_failed': 'PRINT_FAILED',
        'spool_low': 'SPOOL_LOW',
        'maintenance_overdue': 'MAINTENANCE_OVERDUE',
        'info': 'INFO',
        'warning': 'WARNING',
        'critical': 'CRITICAL',
    }
    for old, new in mappings.items():
        cur.execute("UPDATE alerts SET alert_type = ? WHERE alert_type = ?", (new, old))
        cur.execute("UPDATE alerts SET severity = ? WHERE severity = ?", (new, old))
        cur.execute("UPDATE alert_preferences SET alert_type = ? WHERE alert_type = ?", (new, old))

    # Fix NULL booleans
    cur.execute("UPDATE alerts SET is_read = 0 WHERE is_read IS NULL")
    cur.execute("UPDATE alerts SET is_dismissed = 0 WHERE is_dismissed IS NULL")

    conn.commit()
    rows = cur.execute("SELECT id, alert_type, severity, is_read, is_dismissed FROM alerts").fetchall()
    print(f"  DB: Fixed {len(rows)} alert rows. Current data:")
    for r in rows:
        print(f"    id={r[0]} type={r[1]} sev={r[2]} read={r[3]} dismissed={r[4]}")
    conn.close()


def main():
    print("=" * 50)
    print("Hotfix: Alert enum case + nullable booleans")
    print("=" * 50)
    print()
    print("[1/3] Fixing schemas.py...")
    fix_schemas()
    print("[2/3] Fixing mqtt_monitor.py...")
    fix_mqtt_monitor()
    print("[3/3] Fixing existing DB data...")
    fix_existing_data()
    print()
    print("Done! Restart both services:")
    print("  systemctl restart printfarm-backend printfarm-monitor")


if __name__ == "__main__":
    main()
