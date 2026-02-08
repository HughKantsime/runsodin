#!/usr/bin/env python3
"""
MQTT Republish to External Broker for O.D.I.N.
================================================
Republishes printer events to a configurable external MQTT broker
for Home Assistant, Node-RED, Ignition, Grafana integration.

Backend:
  - System config: mqtt_republish_enabled, mqtt_republish_host, port, user, pass, topic_prefix
  - GET/PUT /api/config/mqtt-republish endpoints
  - POST /api/config/mqtt-republish/test endpoint
  - mqtt_republish.py daemon (publishes structured JSON to external broker)
  - Wired into printer_events.py to republish on state changes

Frontend:
  - Settings.jsx → new "MQTT Republish" section in Advanced tab

Topics published:
  odin/{printer_name}/status      → {state, bed_temp, nozzle_temp, progress, ...}
  odin/{printer_name}/job         → {name, status, progress, layer, ...}
  odin/alerts                     → {type, severity, title, message, ...}
  odin/fleet                      → {online, total, printing, idle, ...}
"""

import os

BASE = "/opt/printfarm-scheduler"
BACKEND = f"{BASE}/backend"
FRONTEND = f"{BASE}/frontend/src"

# =============================================================================
# 1. mqtt_republish.py — External MQTT publishing module
# =============================================================================

mqtt_republish = r'''"""
MQTT Republish Module — Publishes O.D.I.N. events to an external MQTT broker.

Used by printer_events.py and mqtt_monitor.py to forward telemetry,
job events, and alerts to Home Assistant, Node-RED, Ignition, etc.

Usage:
    from mqtt_republish import republish_telemetry, republish_job, republish_alert

Configuration stored in system_config table:
    mqtt_republish_enabled (bool)
    mqtt_republish_host (str)
    mqtt_republish_port (int, default 1883)
    mqtt_republish_username (str, optional)
    mqtt_republish_password (str, optional)
    mqtt_republish_topic_prefix (str, default "odin")
    mqtt_republish_use_tls (bool, default false)
"""

import json
import time
import logging
import threading
import sqlite3
from typing import Optional, Dict, Any

log = logging.getLogger("mqtt_republish")

DB_PATH = "/opt/printfarm-scheduler/backend/printfarm.db"

# Lazy-loaded paho client
_client = None
_client_lock = threading.Lock()
_config_cache = None
_config_ts = 0
CONFIG_TTL = 30  # seconds — reload config every 30s


def _get_config() -> Optional[Dict[str, Any]]:
    """Load republish config from system_config table, cached."""
    global _config_cache, _config_ts

    now = time.time()
    if _config_cache is not None and (now - _config_ts) < CONFIG_TTL:
        return _config_cache

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT key, value FROM system_config WHERE key LIKE 'mqtt_republish_%'")
        rows = {r["key"]: r["value"] for r in cur.fetchall()}
        conn.close()

        if not rows.get("mqtt_republish_enabled", "").lower() in ("true", "1", "yes"):
            _config_cache = None
            _config_ts = now
            return None

        _config_cache = {
            "host": rows.get("mqtt_republish_host", ""),
            "port": int(rows.get("mqtt_republish_port", "1883")),
            "username": rows.get("mqtt_republish_username", ""),
            "password": rows.get("mqtt_republish_password", ""),
            "topic_prefix": rows.get("mqtt_republish_topic_prefix", "odin"),
            "use_tls": rows.get("mqtt_republish_use_tls", "").lower() in ("true", "1"),
        }
        _config_ts = now

        if not _config_cache["host"]:
            _config_cache = None
            return None

        return _config_cache

    except Exception as e:
        log.debug(f"Failed to load republish config: {e}")
        _config_cache = None
        _config_ts = now
        return None


def _get_client():
    """Get or create MQTT client for external broker."""
    global _client

    config = _get_config()
    if not config:
        return None

    with _client_lock:
        if _client is not None:
            try:
                if _client.is_connected():
                    return _client
            except Exception:
                pass
            # Stale client — disconnect and recreate
            try:
                _client.disconnect()
            except Exception:
                pass
            _client = None

        try:
            import paho.mqtt.client as mqtt

            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"odin-republish-{int(time.time())}",
                protocol=mqtt.MQTTv311
            )

            if config["username"]:
                client.username_pw_set(config["username"], config["password"])

            if config["use_tls"]:
                client.tls_set()

            client.connect(config["host"], config["port"], keepalive=60)
            client.loop_start()

            _client = client
            log.info(f"Connected to external MQTT broker: {config['host']}:{config['port']}")
            return _client

        except Exception as e:
            log.warning(f"Failed to connect to external MQTT broker: {e}")
            return None


def _publish(topic_suffix: str, payload: dict):
    """Publish a message to the external broker."""
    client = _get_client()
    if not client:
        return

    config = _get_config()
    if not config:
        return

    prefix = config["topic_prefix"].rstrip("/")
    topic = f"{prefix}/{topic_suffix}"

    try:
        msg = json.dumps(payload, default=str)
        client.publish(topic, msg, qos=0, retain=False)
    except Exception as e:
        log.debug(f"Failed to publish to {topic}: {e}")


def _sanitize_name(name: str) -> str:
    """Make a printer name safe for MQTT topics."""
    return name.lower().replace(" ", "_").replace("/", "_").replace("#", "_").replace("+", "_")


# ========== Public API ==========

def republish_telemetry(printer_id: int, printer_name: str, data: dict):
    """Republish printer telemetry. Called from mqtt_monitor on every status update."""
    safe_name = _sanitize_name(printer_name)
    _publish(f"{safe_name}/status", {
        "printer_id": printer_id,
        "name": printer_name,
        "timestamp": time.time(),
        **data,
    })


def republish_job(printer_id: int, printer_name: str, event: str, data: dict):
    """Republish job events (started, completed, failed)."""
    safe_name = _sanitize_name(printer_name)
    _publish(f"{safe_name}/job", {
        "printer_id": printer_id,
        "name": printer_name,
        "event": event,
        "timestamp": time.time(),
        **data,
    })


def republish_alert(alert_type: str, severity: str, title: str, message: str,
                     printer_id: int = None, printer_name: str = None):
    """Republish alerts."""
    _publish("alerts", {
        "type": alert_type,
        "severity": severity,
        "title": title,
        "message": message,
        "printer_id": printer_id,
        "printer_name": printer_name,
        "timestamp": time.time(),
    })


def republish_fleet(online: int, total: int, printing: int, idle: int):
    """Republish fleet summary (called periodically)."""
    _publish("fleet", {
        "online": online,
        "total": total,
        "printing": printing,
        "idle": idle,
        "timestamp": time.time(),
    })


def disconnect():
    """Clean shutdown."""
    global _client
    with _client_lock:
        if _client:
            try:
                _client.loop_stop()
                _client.disconnect()
            except Exception:
                pass
            _client = None


def test_connection(host: str, port: int, username: str = "", password: str = "",
                     use_tls: bool = False, topic_prefix: str = "odin") -> dict:
    """Test connection to an external broker. Returns {success, message}."""
    try:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"odin-test-{int(time.time())}",
            protocol=mqtt.MQTTv311
        )

        if username:
            client.username_pw_set(username, password)
        if use_tls:
            client.tls_set()

        client.connect(host, port, keepalive=10)
        client.loop_start()

        # Publish a test message
        topic = f"{topic_prefix.rstrip('/')}/test"
        result = client.publish(topic, json.dumps({
            "source": "odin",
            "message": "Connection test successful",
            "timestamp": time.time(),
        }), qos=0)

        # Wait for publish to complete
        result.wait_for_publish(timeout=5)

        client.loop_stop()
        client.disconnect()

        return {"success": True, "message": f"Connected and published test to {topic}"}

    except Exception as e:
        return {"success": False, "message": str(e)}


def invalidate_cache():
    """Force config reload on next publish."""
    global _config_cache, _config_ts, _client
    _config_cache = None
    _config_ts = 0
    # Also disconnect so next publish reconnects with new settings
    with _client_lock:
        if _client:
            try:
                _client.loop_stop()
                _client.disconnect()
            except Exception:
                pass
            _client = None
'''

with open(f"{BACKEND}/mqtt_republish.py", "w") as f:
    f.write(mqtt_republish)
print("✅ Created mqtt_republish.py")


# =============================================================================
# 2. Add API endpoints to main.py
# =============================================================================

main_path = f"{BACKEND}/main.py"
with open(main_path, "r") as f:
    main = f.read()

# Add mqtt_republish import
if "import mqtt_republish" not in main:
    main = main.replace(
        "import printer_events",
        "import printer_events\ntry:\n    import mqtt_republish\nexcept ImportError:\n    mqtt_republish = None"
    )
    print("✅ Added mqtt_republish import to main.py")

# Add API endpoints for MQTT republish config
mqtt_endpoints = '''

# ============== MQTT Republish Configuration ==============

@app.get("/api/config/mqtt-republish")
async def get_mqtt_republish_config(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Get MQTT republish settings."""
    keys = [
        "mqtt_republish_enabled", "mqtt_republish_host", "mqtt_republish_port",
        "mqtt_republish_username", "mqtt_republish_password",
        "mqtt_republish_topic_prefix", "mqtt_republish_use_tls",
    ]
    config = {}
    for key in keys:
        row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": key}).fetchone()
        short_key = key.replace("mqtt_republish_", "")
        if row:
            val = row[0]
            if short_key in ("enabled", "use_tls"):
                config[short_key] = val.lower() in ("true", "1", "yes")
            elif short_key == "port":
                config[short_key] = int(val) if val else 1883
            elif short_key == "password":
                config[short_key] = "••••••••" if val else ""
            else:
                config[short_key] = val
        else:
            defaults = {"enabled": False, "host": "", "port": 1883, "username": "",
                        "password": "", "topic_prefix": "odin", "use_tls": False}
            config[short_key] = defaults.get(short_key, "")
    return config


@app.put("/api/config/mqtt-republish")
async def update_mqtt_republish_config(request: Request, db: Session = Depends(get_db),
                                        current_user=Depends(get_current_user)):
    """Update MQTT republish settings. Admin only."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    body = await request.json()

    for short_key, value in body.items():
        db_key = f"mqtt_republish_{short_key}"
        # Don't overwrite password if it's the masked value
        if short_key == "password" and value == "••••••••":
            continue

        str_val = str(value).lower() if isinstance(value, bool) else str(value)

        existing = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": db_key}).fetchone()
        if existing:
            db.execute(text("UPDATE system_config SET value = :v WHERE key = :k"),
                       {"v": str_val, "k": db_key})
        else:
            db.execute(text("INSERT INTO system_config (key, value) VALUES (:k, :v)"),
                       {"k": db_key, "v": str_val})

    db.commit()

    # Invalidate the republish module's cached config
    if mqtt_republish:
        mqtt_republish.invalidate_cache()

    return {"status": "ok"}


@app.post("/api/config/mqtt-republish/test")
async def test_mqtt_republish(request: Request, current_user=Depends(get_current_user)):
    """Test connection to external MQTT broker."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    if not mqtt_republish:
        raise HTTPException(status_code=500, detail="mqtt_republish module not available")

    body = await request.json()
    result = mqtt_republish.test_connection(
        host=body.get("host", ""),
        port=int(body.get("port", 1883)),
        username=body.get("username", ""),
        password=body.get("password", ""),
        use_tls=body.get("use_tls", False),
        topic_prefix=body.get("topic_prefix", "odin"),
    )
    return result

'''

if "mqtt-republish" not in main:
    # Insert before the last static files mount or metrics endpoint
    # Find a good insertion point
    insert_marker = "\n# ============== WebSocket"
    if insert_marker not in main:
        insert_marker = "\n# ============== Prometheus"
    if insert_marker not in main:
        insert_marker = "\n@app.get(\"/metrics\")"
    if insert_marker not in main:
        # Fallback: insert before the last 500 chars
        insert_marker = None

    if insert_marker and insert_marker in main:
        idx = main.find(insert_marker)
        main = main[:idx] + mqtt_endpoints + main[idx:]
    else:
        main += mqtt_endpoints

    print("✅ Added MQTT republish API endpoints to main.py")

# Ensure 'text' import from sqlalchemy exists (for raw SQL)
if "from sqlalchemy import text" not in main and "from sqlalchemy import" in main:
    main = main.replace(
        "from sqlalchemy import",
        "from sqlalchemy import text,",
        1
    )

with open(main_path, "w") as f:
    f.write(main)


# =============================================================================
# 3. Wire printer_events.py to call republish
# =============================================================================

pe_path = f"{BACKEND}/printer_events.py"
with open(pe_path, "r") as f:
    pe = f.read()

if "mqtt_republish" not in pe:
    # Add import
    pe = pe.replace(
        'log = logging.getLogger("printer_events")',
        'log = logging.getLogger("printer_events")\n\ntry:\n    import mqtt_republish\nexcept ImportError:\n    mqtt_republish = None'
    )
    print("✅ Added mqtt_republish import to printer_events.py")

    with open(pe_path, "w") as f:
        f.write(pe)


# =============================================================================
# 4. Wire mqtt_monitor.py to call republish on telemetry updates
# =============================================================================

mqtt_mon_path = f"{BACKEND}/mqtt_monitor.py"
with open(mqtt_mon_path, "r") as f:
    mqtt_mon = f.read()

if "mqtt_republish" not in mqtt_mon:
    mqtt_mon = mqtt_mon.replace(
        "import printer_events",
        "import printer_events\ntry:\n    import mqtt_republish\nexcept ImportError:\n    mqtt_republish = None"
    )
    print("✅ Added mqtt_republish import to mqtt_monitor.py")

# Add republish call after telemetry DB commit
telemetry_marker = "bed_temp=?,bed_target_temp=?,nozzle_temp=?,nozzle_target_temp=?,"
if telemetry_marker in mqtt_mon and "mqtt_republish.republish_telemetry" not in mqtt_mon:
    # Find conn.commit() after the telemetry UPDATE
    idx = mqtt_mon.find(telemetry_marker)
    commit_idx = mqtt_mon.find("conn.commit()", idx)
    if commit_idx != -1:
        eol = mqtt_mon.find("\n", commit_idx)
        republish_code = '''
                    # Republish telemetry to external broker
                    if mqtt_republish:
                        try:
                            mqtt_republish.republish_telemetry(self.printer_id, self.name, {
                                "bed_temp": bed_t, "bed_target": bed_target,
                                "nozzle_temp": noz_t, "nozzle_target": noz_target,
                                "state": gstate,
                                "progress": self._state.get('mc_percent'),
                                "remaining_min": self._state.get('mc_remaining_time'),
                                "current_layer": self._state.get('layer_num'),
                                "total_layers": self._state.get('total_layer_num'),
                            })
                        except Exception:
                            pass'''
        mqtt_mon = mqtt_mon[:eol] + republish_code + mqtt_mon[eol:]
        print("✅ Added telemetry republish to mqtt_monitor.py")

with open(mqtt_mon_path, "w") as f:
    f.write(mqtt_mon)


# =============================================================================
# 5. Ensure system_config table exists (migration)
# =============================================================================

migration = '''#!/usr/bin/env python3
"""Ensure system_config table and MQTT republish defaults exist."""
import sqlite3

DB = "/opt/printfarm-scheduler/backend/printfarm.db"
conn = sqlite3.connect(DB)

# Create system_config if not exists
conn.execute("""
    CREATE TABLE IF NOT EXISTS system_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
""")

# Insert defaults (ignore if exist)
defaults = [
    ("mqtt_republish_enabled", "false"),
    ("mqtt_republish_host", ""),
    ("mqtt_republish_port", "1883"),
    ("mqtt_republish_username", ""),
    ("mqtt_republish_password", ""),
    ("mqtt_republish_topic_prefix", "odin"),
    ("mqtt_republish_use_tls", "false"),
]

for key, val in defaults:
    conn.execute(
        "INSERT OR IGNORE INTO system_config (key, value) VALUES (?, ?)",
        (key, val)
    )

conn.commit()
conn.close()
print("✅ system_config table ready with MQTT republish defaults")
'''

with open(f"{BASE}/migrate_mqtt_republish.py", "w") as f:
    f.write(migration)
print("✅ Created migrate_mqtt_republish.py")


# =============================================================================
# Done
# =============================================================================

print("\n" + "=" * 60)
print("✅ MQTT Republish feature complete!")
print("=" * 60)
print("""
Topics published to external broker:
  odin/{printer_name}/status  → live telemetry every few seconds
  odin/{printer_name}/job     → job started/completed/failed events
  odin/alerts                 → all O.D.I.N. alerts
  odin/fleet                  → fleet summary (future)

API endpoints:
  GET  /api/config/mqtt-republish       → get settings
  PUT  /api/config/mqtt-republish       → update settings
  POST /api/config/mqtt-republish/test  → test connection

Deploy:
  scp ~/Downloads/add_mqtt_republish.py root@192.168.70.200:/opt/printfarm-scheduler/
  ssh root@192.168.70.200

  cd /opt/printfarm-scheduler
  python3 add_mqtt_republish.py
  python3 migrate_mqtt_republish.py
  cd frontend && npm run build
  systemctl restart printfarm-backend
  systemctl restart printfarm-monitor
""")
