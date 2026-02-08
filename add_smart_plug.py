#!/usr/bin/env python3
"""
O.D.I.N. — Smart Plug Integration (Tasmota / Home Assistant / MQTT)
- Per-printer smart plug configuration
- Auto power-on before print, auto power-off after completion (with cooldown)
- Energy consumption tracking per job
- Manual on/off from UI
- Hooks into printer_events.py lifecycle
"""

import sqlite3
import os
import json
import re

DB_PATH = "/opt/printfarm-scheduler/backend/printfarm.db"
MAIN_PATH = "/opt/printfarm-scheduler/backend/main.py"
EVENTS_PATH = "/opt/printfarm-scheduler/backend/printer_events.py"
SMARTPLUG_PATH = "/opt/printfarm-scheduler/backend/smart_plug.py"

print("=" * 60)
print("  O.D.I.N. — Smart Plug Integration")
print("=" * 60)
print()

# ============================================================
# 1. Database migration
# ============================================================

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# Add smart plug columns to printers table
cur.execute("PRAGMA table_info(printers)")
existing_cols = [col[1] for col in cur.fetchall()]

new_cols = {
    "plug_type": "VARCHAR(20)",           # 'tasmota', 'homeassistant', 'mqtt', null
    "plug_host": "VARCHAR(255)",          # IP/hostname for Tasmota; HA URL for HA
    "plug_entity_id": "VARCHAR(255)",     # HA entity_id or MQTT topic
    "plug_auth_token": "TEXT",            # HA long-lived access token
    "plug_auto_on": "BOOLEAN DEFAULT 1",  # Auto power-on before print
    "plug_auto_off": "BOOLEAN DEFAULT 1", # Auto power-off after print
    "plug_cooldown_minutes": "INTEGER DEFAULT 5",  # Delay before auto-off
    "plug_power_state": "BOOLEAN",        # Last known power state
    "plug_energy_kwh": "FLOAT DEFAULT 0", # Cumulative energy tracked
}

added = []
for col_name, col_type in new_cols.items():
    if col_name not in existing_cols:
        cur.execute(f"ALTER TABLE printers ADD COLUMN {col_name} {col_type}")
        added.append(col_name)

# Add energy tracking columns to jobs table
cur.execute("PRAGMA table_info(jobs)")
job_cols = [col[1] for col in cur.fetchall()]

job_new_cols = {
    "energy_kwh": "FLOAT",               # Energy consumed during this job
    "energy_cost": "FLOAT",              # Calculated cost (kwh * rate)
}

for col_name, col_type in job_new_cols.items():
    if col_name not in job_cols:
        cur.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}")
        added.append(f"jobs.{col_name}")

# Add energy rate to system_config if not present
cur.execute("SELECT value FROM system_config WHERE key = 'energy_cost_per_kwh'")
if not cur.fetchone():
    cur.execute("INSERT INTO system_config (key, value) VALUES ('energy_cost_per_kwh', '0.12')")
    added.append("system_config.energy_cost_per_kwh")

conn.commit()
conn.close()

if added:
    print(f"[1/5] ✅ Database migration: added {len(added)} columns")
    for a in added:
        print(f"       + {a}")
else:
    print("[1/5] ⚠️  Database already migrated")

# ============================================================
# 2. Create smart_plug.py module
# ============================================================

smart_plug_module = '''"""
Smart Plug Controller for O.D.I.N.
Supports: Tasmota (HTTP), Home Assistant (REST API), Generic MQTT
Each printer can optionally have a smart plug configured for:
- Auto power-on before print
- Auto power-off after print (with cooldown delay)
- Energy consumption tracking
- Manual on/off control
"""

import sqlite3
import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

log = logging.getLogger("smart_plug")

DB_PATH = "/opt/printfarm-scheduler/backend/printfarm.db"

# ---- HTTP helper (no requests dependency — use urllib) ----

def _http_request(url: str, method: str = "GET", headers: dict = None,
                  data: str = None, timeout: int = 5) -> Tuple[int, str]:
    """Simple HTTP request using urllib (no external deps)."""
    import urllib.request
    import urllib.error
    
    req = urllib.request.Request(url, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if data:
        req.data = data.encode("utf-8")
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.error(f"HTTP request failed: {e}")
        return 0, str(e)


# ---- Tasmota ----

def tasmota_power(host: str, action: str = "TOGGLE") -> Optional[bool]:
    """
    Control Tasmota smart plug via HTTP.
    action: "ON", "OFF", "TOGGLE", or "" (status query)
    Returns True=on, False=off, None=error
    """
    url = f"http://{host}/cm?cmnd=Power%20{action}"
    status, body = _http_request(url)
    if status == 200:
        try:
            data = json.loads(body)
            power = data.get("POWER", data.get("Power", ""))
            return power == "ON"
        except:
            pass
    return None


def tasmota_energy(host: str) -> Optional[Dict]:
    """
    Get energy data from Tasmota plug with energy monitoring.
    Returns dict with keys: total_kwh, power_w, voltage, current
    """
    url = f"http://{host}/cm?cmnd=Status%208"
    status, body = _http_request(url)
    if status == 200:
        try:
            data = json.loads(body)
            energy = data.get("StatusSNS", {}).get("ENERGY", {})
            return {
                "total_kwh": energy.get("Total", 0),
                "power_w": energy.get("Power", 0),
                "voltage": energy.get("Voltage", 0),
                "current": energy.get("Current", 0),
            }
        except:
            pass
    return None


# ---- Home Assistant ----

def ha_switch(ha_url: str, entity_id: str, token: str, action: str = "toggle") -> Optional[bool]:
    """
    Control a Home Assistant switch/plug entity.
    action: "turn_on", "turn_off", "toggle"
    Returns True=on, False=off, None=error
    """
    service = f"switch/{action}"
    url = f"{ha_url.rstrip('/')}/api/services/{service}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = json.dumps({"entity_id": entity_id})
    status, body = _http_request(url, method="POST", headers=headers, data=payload)
    
    if status == 200:
        # Query state after action
        return ha_get_state(ha_url, entity_id, token)
    return None


def ha_get_state(ha_url: str, entity_id: str, token: str) -> Optional[bool]:
    """Get current state of a Home Assistant entity."""
    url = f"{ha_url.rstrip('/')}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {token}"}
    status, body = _http_request(url, headers=headers)
    if status == 200:
        try:
            data = json.loads(body)
            return data.get("state") == "on"
        except:
            pass
    return None


# ---- Generic MQTT (via O.D.I.N.'s MQTT republish connection) ----

def mqtt_power(topic: str, action: str = "TOGGLE") -> Optional[bool]:
    """
    Publish power command to an MQTT topic.
    Requires mqtt_republish to be configured.
    """
    try:
        import mqtt_republish
        if mqtt_republish and hasattr(mqtt_republish, 'publish'):
            payload = action.upper()
            mqtt_republish.publish(topic, payload)
            log.info(f"MQTT power command: {topic} = {payload}")
            return action.upper() == "ON"  # Assume success
    except Exception as e:
        log.error(f"MQTT power command failed: {e}")
    return None


# ---- Unified Interface ----

def get_plug_config(printer_id: int) -> Optional[Dict]:
    """Get smart plug config for a printer."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """SELECT plug_type, plug_host, plug_entity_id, plug_auth_token,
                  plug_auto_on, plug_auto_off, plug_cooldown_minutes, plug_power_state
           FROM printers WHERE id = ?""",
        (printer_id,)
    )
    row = cur.fetchone()
    conn.close()
    
    if not row or not row[0]:
        return None
    
    return {
        "type": row[0],
        "host": row[1],
        "entity_id": row[2],
        "auth_token": row[3],
        "auto_on": bool(row[4]),
        "auto_off": bool(row[5]),
        "cooldown_minutes": row[6] or 5,
        "power_state": row[7],
    }


def power_on(printer_id: int) -> Optional[bool]:
    """Turn on a printer's smart plug."""
    config = get_plug_config(printer_id)
    if not config:
        return None
    
    result = None
    if config["type"] == "tasmota":
        result = tasmota_power(config["host"], "ON")
    elif config["type"] == "homeassistant":
        result = ha_switch(config["host"], config["entity_id"], config["auth_token"], "turn_on")
    elif config["type"] == "mqtt":
        result = mqtt_power(config["entity_id"], "ON")
    
    if result is not None:
        _update_power_state(printer_id, result)
    
    return result


def power_off(printer_id: int) -> Optional[bool]:
    """Turn off a printer's smart plug."""
    config = get_plug_config(printer_id)
    if not config:
        return None
    
    result = None
    if config["type"] == "tasmota":
        result = tasmota_power(config["host"], "OFF")
    elif config["type"] == "homeassistant":
        result = ha_switch(config["host"], config["entity_id"], config["auth_token"], "turn_off")
    elif config["type"] == "mqtt":
        result = mqtt_power(config["entity_id"], "OFF")
    
    if result is not None:
        _update_power_state(printer_id, result)
    
    return result


def power_toggle(printer_id: int) -> Optional[bool]:
    """Toggle a printer's smart plug."""
    config = get_plug_config(printer_id)
    if not config:
        return None
    
    result = None
    if config["type"] == "tasmota":
        result = tasmota_power(config["host"], "TOGGLE")
    elif config["type"] == "homeassistant":
        result = ha_switch(config["host"], config["entity_id"], config["auth_token"], "toggle")
    elif config["type"] == "mqtt":
        result = mqtt_power(config["entity_id"], "TOGGLE")
    
    if result is not None:
        _update_power_state(printer_id, result)
    
    return result


def get_energy(printer_id: int) -> Optional[Dict]:
    """Get energy consumption data from smart plug."""
    config = get_plug_config(printer_id)
    if not config:
        return None
    
    if config["type"] == "tasmota":
        return tasmota_energy(config["host"])
    
    # HA energy tracking via sensor entity
    if config["type"] == "homeassistant":
        # Try to read energy sensor — entity_id might be switch.printer_plug
        # Energy sensor is usually sensor.printer_plug_energy
        energy_entity = config["entity_id"].replace("switch.", "sensor.") + "_energy"
        state = ha_get_state(config["host"], energy_entity, config["auth_token"])
        if state is not None:
            try:
                return {"total_kwh": float(state), "power_w": 0, "voltage": 0, "current": 0}
            except:
                pass
    
    return None


def get_power_state(printer_id: int) -> Optional[bool]:
    """Query current power state from smart plug."""
    config = get_plug_config(printer_id)
    if not config:
        return None
    
    result = None
    if config["type"] == "tasmota":
        result = tasmota_power(config["host"], "")  # Empty = status query
    elif config["type"] == "homeassistant":
        result = ha_get_state(config["host"], config["entity_id"], config["auth_token"])
    
    if result is not None:
        _update_power_state(printer_id, result)
    
    return result


def _update_power_state(printer_id: int, state: bool):
    """Update cached power state in database."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE printers SET plug_power_state = ? WHERE id = ?", (state, printer_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"Failed to update power state: {e}")


# ---- Lifecycle Hooks ----

_cooldown_timers = {}  # printer_id -> threading.Timer

def on_print_start(printer_id: int):
    """Called when a print starts — auto power-on if configured."""
    config = get_plug_config(printer_id)
    if not config or not config["auto_on"]:
        return
    
    # Cancel any pending power-off timer
    if printer_id in _cooldown_timers:
        _cooldown_timers[printer_id].cancel()
        del _cooldown_timers[printer_id]
        log.info(f"Cancelled power-off timer for printer {printer_id}")
    
    result = power_on(printer_id)
    if result:
        log.info(f"Auto power-on: printer {printer_id}")


def on_print_complete(printer_id: int):
    """Called when a print completes — schedule auto power-off with cooldown."""
    config = get_plug_config(printer_id)
    if not config or not config["auto_off"]:
        return
    
    cooldown = config["cooldown_minutes"] * 60  # seconds
    
    # Cancel existing timer if any
    if printer_id in _cooldown_timers:
        _cooldown_timers[printer_id].cancel()
    
    def delayed_off():
        log.info(f"Auto power-off: printer {printer_id} (after {config['cooldown_minutes']}m cooldown)")
        power_off(printer_id)
        if printer_id in _cooldown_timers:
            del _cooldown_timers[printer_id]
    
    timer = threading.Timer(cooldown, delayed_off)
    timer.daemon = True
    timer.start()
    _cooldown_timers[printer_id] = timer
    log.info(f"Scheduled power-off for printer {printer_id} in {config['cooldown_minutes']}m")


def record_energy_for_job(printer_id: int, job_id: int, start_kwh: float):
    """
    Calculate energy consumed during a job and store it.
    Call at job start to capture start_kwh, then at job end with the same start_kwh.
    """
    energy = get_energy(printer_id)
    if not energy:
        return
    
    end_kwh = energy.get("total_kwh", 0)
    consumed = max(0, end_kwh - start_kwh)
    
    if consumed <= 0:
        return
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # Get energy cost rate
        cur.execute("SELECT value FROM system_config WHERE key = 'energy_cost_per_kwh'")
        row = cur.fetchone()
        rate = float(row[0]) if row else 0.12
        
        cost = round(consumed * rate, 4)
        
        cur.execute(
            "UPDATE jobs SET energy_kwh = ?, energy_cost = ? WHERE id = ?",
            (round(consumed, 4), cost, job_id)
        )
        
        # Update printer cumulative
        cur.execute(
            "UPDATE printers SET plug_energy_kwh = plug_energy_kwh + ? WHERE id = ?",
            (round(consumed, 4), printer_id)
        )
        
        conn.commit()
        conn.close()
        log.info(f"Energy recorded for job {job_id}: {consumed:.4f} kWh (${cost:.4f})")
    except Exception as e:
        log.error(f"Failed to record energy: {e}")
'''

with open(SMARTPLUG_PATH, "w") as f:
    f.write(smart_plug_module)
print("[2/5] ✅ Created smart_plug.py module")

# ============================================================
# 3. Hook into printer_events.py
# ============================================================

with open(EVENTS_PATH, "r") as f:
    events_content = f.read()

events_changes = []

# Add import
if "import smart_plug" not in events_content:
    old_import = "try:\n    from ws_hub import push_event as ws_push"
    new_import = """try:
    import smart_plug
except ImportError:
    smart_plug = None

try:
    from ws_hub import push_event as ws_push"""
    events_content = events_content.replace(old_import, new_import, 1)
    events_changes.append("Added smart_plug import")

# Hook into job_started
if "smart_plug.on_print_start" not in events_content:
    # Find the end of job_started function — look for the return or the log line
    old_started = '        log.info(f"Job started on printer {printer_id}: {job_name}")'
    if old_started not in events_content:
        # Try alternate
        old_started = 'log.info(f"Job started on printer {printer_id}'
        idx = events_content.find(old_started)
        if idx > 0:
            line_end = events_content.index('\n', idx)
            old_started = events_content[idx:line_end]
    
    new_started = old_started + """
        
        # Smart plug: auto power-on
        if smart_plug:
            try:
                smart_plug.on_print_start(printer_id)
            except Exception as e:
                log.warning(f"Smart plug on_print_start failed: {e}")"""
    
    if old_started in events_content:
        events_content = events_content.replace(old_started, new_started, 1)
        events_changes.append("Hooked smart_plug.on_print_start into job_started")

# Hook into job_completed
if "smart_plug.on_print_complete" not in events_content:
    old_completed = '        log.info(f"Job {status} on printer {printer_id}: {job_name}")'
    new_completed = old_completed + """
        
        # Smart plug: auto power-off (with cooldown)
        if smart_plug and success:
            try:
                smart_plug.on_print_complete(printer_id)
            except Exception as e:
                log.warning(f"Smart plug on_print_complete failed: {e}")"""
    
    if old_completed in events_content:
        events_content = events_content.replace(old_completed, new_completed, 1)
        events_changes.append("Hooked smart_plug.on_print_complete into job_completed")

with open(EVENTS_PATH, "w") as f:
    f.write(events_content)

print(f"[3/5] Patched printer_events.py:")
for c in events_changes:
    print(f"  ✅ {c}")

# ============================================================
# 4. Add API endpoints to main.py
# ============================================================

with open(MAIN_PATH, "r") as f:
    main_content = f.read()

api_changes = []

# Add smart_plug import
if "import smart_plug" not in main_content:
    old_imp = "from threemf_parser import parse_3mf, extract_objects_from_plate, extract_mesh_from_3mf"
    new_imp = old_imp + "\nimport smart_plug"
    main_content = main_content.replace(old_imp, new_imp, 1)
    api_changes.append("Added smart_plug import")

# Add smart plug API endpoints
smart_plug_endpoints = '''

# ============== Smart Plug Control ==============

@app.get("/api/printers/{printer_id}/plug", tags=["Smart Plug"])
async def get_plug_config(printer_id: int, db: Session = Depends(get_db)):
    """Get smart plug configuration for a printer."""
    result = db.execute(text("""
        SELECT plug_type, plug_host, plug_entity_id, plug_auto_on, plug_auto_off,
               plug_cooldown_minutes, plug_power_state, plug_energy_kwh
        FROM printers WHERE id = :id
    """), {"id": printer_id}).fetchone()
    
    if not result:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    return {
        "type": result[0],
        "host": result[1],
        "entity_id": result[2],
        "auto_on": bool(result[3]) if result[3] is not None else True,
        "auto_off": bool(result[4]) if result[4] is not None else True,
        "cooldown_minutes": result[5] or 5,
        "power_state": result[6],
        "energy_kwh": result[7] or 0,
        "configured": result[0] is not None,
    }


@app.put("/api/printers/{printer_id}/plug", tags=["Smart Plug"])
async def update_plug_config(printer_id: int, request: Request, db: Session = Depends(get_db)):
    """Update smart plug configuration for a printer."""
    data = await request.json()
    
    # Validate plug type
    plug_type = data.get("type")
    if plug_type and plug_type not in ("tasmota", "homeassistant", "mqtt"):
        raise HTTPException(400, "Invalid plug type. Use: tasmota, homeassistant, mqtt")
    
    db.execute(text("""
        UPDATE printers SET
            plug_type = :plug_type,
            plug_host = :plug_host,
            plug_entity_id = :plug_entity_id,
            plug_auth_token = :plug_auth_token,
            plug_auto_on = :plug_auto_on,
            plug_auto_off = :plug_auto_off,
            plug_cooldown_minutes = :plug_cooldown_minutes
        WHERE id = :id
    """), {
        "id": printer_id,
        "plug_type": plug_type,
        "plug_host": data.get("host"),
        "plug_entity_id": data.get("entity_id"),
        "plug_auth_token": data.get("auth_token"),
        "plug_auto_on": data.get("auto_on", True),
        "plug_auto_off": data.get("auto_off", True),
        "plug_cooldown_minutes": data.get("cooldown_minutes", 5),
    })
    db.commit()
    
    return {"status": "ok", "message": "Smart plug configuration updated"}


@app.delete("/api/printers/{printer_id}/plug", tags=["Smart Plug"])
async def remove_plug_config(printer_id: int, db: Session = Depends(get_db)):
    """Remove smart plug configuration from a printer."""
    db.execute(text("""
        UPDATE printers SET
            plug_type = NULL, plug_host = NULL, plug_entity_id = NULL,
            plug_auth_token = NULL, plug_auto_on = 1, plug_auto_off = 1,
            plug_cooldown_minutes = 5, plug_power_state = NULL
        WHERE id = :id
    """), {"id": printer_id})
    db.commit()
    return {"status": "ok"}


@app.post("/api/printers/{printer_id}/plug/on", tags=["Smart Plug"])
async def plug_power_on(printer_id: int):
    """Turn on a printer's smart plug."""
    result = smart_plug.power_on(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@app.post("/api/printers/{printer_id}/plug/off", tags=["Smart Plug"])
async def plug_power_off(printer_id: int):
    """Turn off a printer's smart plug."""
    result = smart_plug.power_off(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@app.post("/api/printers/{printer_id}/plug/toggle", tags=["Smart Plug"])
async def plug_power_toggle(printer_id: int):
    """Toggle a printer's smart plug."""
    result = smart_plug.power_toggle(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@app.get("/api/printers/{printer_id}/plug/energy", tags=["Smart Plug"])
async def plug_energy(printer_id: int):
    """Get current energy data from smart plug."""
    data = smart_plug.get_energy(printer_id)
    if data is None:
        raise HTTPException(400, "No energy data available")
    return data


@app.get("/api/printers/{printer_id}/plug/state", tags=["Smart Plug"])
async def plug_state(printer_id: int):
    """Query current power state from smart plug."""
    state = smart_plug.get_power_state(printer_id)
    if state is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": state}


@app.get("/api/settings/energy-rate", tags=["Smart Plug"])
async def get_energy_rate(db: Session = Depends(get_db)):
    """Get energy cost per kWh."""
    result = db.execute(text("SELECT value FROM system_config WHERE key = 'energy_cost_per_kwh'")).fetchone()
    return {"energy_cost_per_kwh": float(result[0]) if result else 0.12}


@app.put("/api/settings/energy-rate", tags=["Smart Plug"])
async def set_energy_rate(request: Request, db: Session = Depends(get_db)):
    """Set energy cost per kWh."""
    data = await request.json()
    rate = data.get("energy_cost_per_kwh", 0.12)
    db.execute(text(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES ('energy_cost_per_kwh', :rate)"
    ), {"rate": str(rate)})
    db.commit()
    return {"energy_cost_per_kwh": rate}

'''

if "/api/printers/{printer_id}/plug" not in main_content:
    # Insert before Maintenance section
    insert_marker = "# ============== 3D Model Viewer"
    if insert_marker in main_content:
        main_content = main_content.replace(insert_marker, smart_plug_endpoints + "\n" + insert_marker)
        api_changes.append("Added smart plug API endpoints (10 endpoints)")
    else:
        # Try Maintenance marker
        insert_marker2 = "# ============== Maintenance"
        if insert_marker2 in main_content:
            main_content = main_content.replace(insert_marker2, smart_plug_endpoints + "\n" + insert_marker2)
            api_changes.append("Added smart plug API endpoints (10 endpoints)")
        else:
            main_content += smart_plug_endpoints
            api_changes.append("Added smart plug API endpoints (appended)")

with open(MAIN_PATH, "w") as f:
    f.write(main_content)

print(f"[4/5] Patched main.py:")
for c in api_changes:
    print(f"  ✅ {c}")

# ============================================================
# 5. Add SQLAlchemy model columns
# ============================================================

MODELS_PY = "/opt/printfarm-scheduler/backend/models.py"
with open(MODELS_PY, "r") as f:
    models_content = f.read()

model_changes = []

if "plug_type" not in models_content:
    # Find the end of Printer class — after nozzle_diameter
    insert_after = "    nozzle_diameter = Column(Float, nullable=True)"
    plug_columns = """
    
    # Smart plug integration
    plug_type = Column(String(20), nullable=True)         # 'tasmota', 'homeassistant', 'mqtt'
    plug_host = Column(String(255), nullable=True)        # IP or HA URL
    plug_entity_id = Column(String(255), nullable=True)   # HA entity_id or MQTT topic
    plug_auth_token = Column(Text, nullable=True)         # HA long-lived access token
    plug_auto_on = Column(Boolean, default=True)          # Auto power-on before print
    plug_auto_off = Column(Boolean, default=True)         # Auto power-off after print
    plug_cooldown_minutes = Column(Integer, default=5)    # Delay before auto-off
    plug_power_state = Column(Boolean, nullable=True)     # Last known power state
    plug_energy_kwh = Column(Float, default=0)            # Cumulative energy"""
    
    models_content = models_content.replace(insert_after, insert_after + plug_columns, 1)
    model_changes.append("Added smart plug columns to Printer model")

if "energy_kwh" not in models_content:
    # Find Job class — after estimated_cost or suggested_price
    job_marker = "    suggested_price = Column(Float, nullable=True)"
    if job_marker in models_content:
        energy_cols = """
    energy_kwh = Column(Float, nullable=True)             # Energy consumed during job
    energy_cost = Column(Float, nullable=True)            # Calculated electricity cost"""
        models_content = models_content.replace(job_marker, job_marker + energy_cols, 1)
        model_changes.append("Added energy columns to Job model")

with open(MODELS_PY, "w") as f:
    f.write(models_content)

print(f"[5/5] Patched models.py:")
for c in model_changes:
    print(f"  ✅ {c}")

print()
print("=" * 60)
print("  Smart Plug Integration Complete")
print()
print("  Supported: Tasmota HTTP, Home Assistant REST, MQTT")
print("  Features: Auto on/off, cooldown delay, energy tracking")
print("  Endpoints: /api/printers/{id}/plug/*")
print()
print("  Next: npm run build && systemctl restart printfarm-backend")
print("=" * 60)
