import os
"""
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

DB_PATH = os.environ.get('DATABASE_PATH', '/data/odin.db')

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

        if rows.get("mqtt_republish_enabled", "").lower() not in ("true", "1", "yes"):
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
