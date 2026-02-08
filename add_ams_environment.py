#!/usr/bin/env python3
"""
O.D.I.N. — AMS Humidity & Temperature Monitoring
- Creates ams_telemetry table for time-series data
- Captures AMS humidity from MQTT state on heartbeat cycle
- Adds API for historical charts
- Bambu AMS reports: humidity (0-5 scale), temperature (not always available)
"""

import sqlite3
import os
import re

DB_PATH = "/opt/printfarm-scheduler/backend/printfarm.db"
MQTT_PATH = "/opt/printfarm-scheduler/backend/mqtt_monitor.py"
MAIN_PATH = "/opt/printfarm-scheduler/backend/main.py"

print("=" * 60)
print("  O.D.I.N. — AMS Environmental Monitoring")
print("=" * 60)
print()

# ============================================================
# 1. Database migration
# ============================================================

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# Create ams_telemetry table
cur.execute("""
    CREATE TABLE IF NOT EXISTS ams_telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        printer_id INTEGER NOT NULL,
        ams_unit INTEGER NOT NULL DEFAULT 0,
        humidity INTEGER,
        temperature FLOAT,
        recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (printer_id) REFERENCES printers(id)
    )
""")

# Index for efficient querying
cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_ams_telemetry_printer_time 
    ON ams_telemetry(printer_id, recorded_at DESC)
""")

conn.commit()
conn.close()
print("[1/4] ✅ Created ams_telemetry table with index")

# ============================================================
# 2. Patch mqtt_monitor.py to capture AMS environment data
# ============================================================

with open(MQTT_PATH, "r") as f:
    mqtt_content = f.read()

mqtt_changes = []

# Add AMS environment capture to the heartbeat block
# The heartbeat runs every ~10 seconds and updates printer telemetry
# We'll capture AMS humidity at a lower rate (every 5 minutes) to avoid DB bloat

if "ams_telemetry" not in mqtt_content:
    # Find the end of the heartbeat UPDATE printers SET ... block
    # Look for the commit after the telemetry update
    
    ams_capture_code = '''
                    # ---- AMS Environmental Data Capture ----
                    # Record AMS humidity every 5 minutes (not every heartbeat)
                    if time.time() - getattr(self, '_last_ams_env', 0) >= 300:
                        self._last_ams_env = time.time()
                        try:
                            ams_raw = self._state.get('ams', {})
                            ams_units = ams_raw.get('ams', []) if isinstance(ams_raw, dict) else []
                            for unit_idx, unit in enumerate(ams_units):
                                if isinstance(unit, dict):
                                    humidity = unit.get('humidity')
                                    temperature = unit.get('temp')
                                    # Bambu reports humidity as string "1"-"5" or int
                                    if humidity is not None:
                                        try:
                                            humidity = int(humidity)
                                        except (ValueError, TypeError):
                                            humidity = None
                                    if temperature is not None:
                                        try:
                                            temperature = float(temperature)
                                        except (ValueError, TypeError):
                                            temperature = None
                                    if humidity is not None or temperature is not None:
                                        conn.execute(
                                            "INSERT INTO ams_telemetry (printer_id, ams_unit, humidity, temperature) VALUES (?, ?, ?, ?)",
                                            (self.printer_id, unit_idx, humidity, temperature)
                                        )
                            # Prune old data (keep 7 days)
                            conn.execute(
                                "DELETE FROM ams_telemetry WHERE recorded_at < datetime('now', '-7 days')"
                            )
                            conn.commit()
                        except Exception as e:
                            log.debug(f"[{self.name}] AMS env capture: {e}")
'''
    
    # Insert after the heartbeat commit
    # Find: conn.commit() that follows the big UPDATE printers SET block
    # The pattern is: after "self._last_heartbeat = time.time()"
    heartbeat_marker = "self._last_heartbeat = time.time()"
    if heartbeat_marker in mqtt_content:
        idx = mqtt_content.index(heartbeat_marker)
        line_end = mqtt_content.index('\n', idx)
        mqtt_content = mqtt_content[:line_end+1] + ams_capture_code + mqtt_content[line_end+1:]
        mqtt_changes.append("Added AMS humidity/temperature capture (every 5 min)")

    with open(MQTT_PATH, "w") as f:
        f.write(mqtt_content)

print(f"[2/4] Patched mqtt_monitor.py:")
for c in mqtt_changes:
    print(f"  ✅ {c}")
if not mqtt_changes:
    print("  ⚠️  Already patched or marker not found")

# ============================================================
# 3. Add API endpoints to main.py
# ============================================================

with open(MAIN_PATH, "r") as f:
    main_content = f.read()

api_changes = []

ams_endpoints = '''

# ============== AMS Environmental Monitoring ==============

@app.get("/api/printers/{printer_id}/ams/environment", tags=["AMS"])
async def get_ams_environment(
    printer_id: int,
    hours: int = Query(default=24, ge=1, le=168),
    unit: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """
    Get AMS humidity/temperature history for charts.
    Returns time-series data for the specified time window.
    Default: last 24 hours. Max: 7 days (168 hours).
    """
    query = """
        SELECT ams_unit, humidity, temperature, recorded_at
        FROM ams_telemetry
        WHERE printer_id = :printer_id
        AND recorded_at >= datetime('now', :hours_ago)
    """
    params = {
        "printer_id": printer_id,
        "hours_ago": f"-{hours} hours"
    }
    
    if unit is not None:
        query += " AND ams_unit = :unit"
        params["unit"] = unit
    
    query += " ORDER BY recorded_at ASC"
    
    rows = db.execute(text(query), params).fetchall()
    
    # Group by AMS unit
    units = {}
    for row in rows:
        u = row[0]
        if u not in units:
            units[u] = []
        units[u].append({
            "humidity": row[1],
            "temperature": row[2],
            "time": row[3]
        })
    
    return {
        "printer_id": printer_id,
        "hours": hours,
        "units": units
    }


@app.get("/api/printers/{printer_id}/ams/current", tags=["AMS"])
async def get_ams_current(printer_id: int, db: Session = Depends(get_db)):
    """
    Get latest AMS environmental readings for a printer.
    Returns the most recent humidity/temperature per AMS unit.
    """
    rows = db.execute(text("""
        SELECT ams_unit, humidity, temperature, recorded_at
        FROM ams_telemetry
        WHERE printer_id = :pid
        AND recorded_at = (
            SELECT MAX(recorded_at) FROM ams_telemetry t2
            WHERE t2.printer_id = ams_telemetry.printer_id
            AND t2.ams_unit = ams_telemetry.ams_unit
        )
        ORDER BY ams_unit
    """), {"pid": printer_id}).fetchall()
    
    units = []
    for row in rows:
        # Map Bambu humidity scale: 1=dry, 5=wet
        hum = row[1]
        hum_label = {1: "Dry", 2: "Low", 3: "Moderate", 4: "High", 5: "Wet"}.get(hum, "Unknown") if hum else "N/A"
        units.append({
            "unit": row[0],
            "humidity": hum,
            "humidity_label": hum_label,
            "temperature": row[2],
            "recorded_at": row[3]
        })
    
    return {"printer_id": printer_id, "units": units}

'''

if "/api/printers/{printer_id}/ams/environment" not in main_content:
    # Insert before Smart Plug section or 3D Viewer section
    for marker in ["# ============== Smart Plug", "# ============== 3D Model Viewer", "# ============== Maintenance"]:
        if marker in main_content:
            main_content = main_content.replace(marker, ams_endpoints + "\n" + marker)
            api_changes.append("Added AMS environment API endpoints")
            break
    else:
        main_content += ams_endpoints
        api_changes.append("Added AMS environment API endpoints (appended)")

with open(MAIN_PATH, "w") as f:
    f.write(main_content)

print(f"[3/4] Patched main.py:")
for c in api_changes:
    print(f"  ✅ {c}")

# ============================================================
# 4. Add API function to frontend api.js
# ============================================================

API_JS = "/opt/printfarm-scheduler/frontend/src/api.js"
with open(API_JS, "r") as f:
    api_js = f.read()

if "getAmsEnvironment" not in api_js:
    api_js += '''
// ---- AMS Environmental Monitoring ----
export const getAmsEnvironment = (printerId, hours = 24, unit = null) => {
  let url = `/printers/${printerId}/ams/environment?hours=${hours}`
  if (unit !== null) url += `&unit=${unit}`
  return fetchAPI(url)
}
export const getAmsCurrent = (printerId) => fetchAPI(`/printers/${printerId}/ams/current`)
'''
    with open(API_JS, "w") as f:
        f.write(api_js)
    print("[4/4] ✅ Added AMS API functions to api.js")
else:
    print("[4/4] ⚠️  AMS API functions already in api.js")

print()
print("=" * 60)
print("  AMS Environmental Monitoring Complete")
print()
print("  Data capture: Every 5 minutes per AMS unit (7-day retention)")
print("  Bambu humidity scale: 1=Dry, 2=Low, 3=Moderate, 4=High, 5=Wet")
print("  Endpoints:")
print("    GET /api/printers/{id}/ams/environment?hours=24")
print("    GET /api/printers/{id}/ams/current")
print()
print("  Next: npm run build && systemctl restart printfarm-backend")
print("=" * 60)
