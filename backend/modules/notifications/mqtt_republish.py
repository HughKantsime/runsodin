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
from typing import Optional, Dict, Any

from sqlalchemy import text

from core.db import engine

log = logging.getLogger("mqtt_republish")

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
        with engine.connect() as conn:
            result = conn.execute(text("SELECT key, value FROM system_config WHERE key LIKE 'mqtt_republish_%'"))
            rows = {r._mapping["key"]: r._mapping["value"] for r in result.fetchall()}

        if rows.get("mqtt_republish_enabled", "").lower() not in ("true", "1", "yes"):
            _config_cache = None
            _config_ts = now
            return None

        raw_password = rows.get("mqtt_republish_password", "")
        # Decrypt password — migration-safe: crypto.decrypt() falls back to raw on failure
        if raw_password:
            try:
                import core.crypto as crypto
                raw_password = crypto.decrypt(raw_password)
            except Exception as e:
                log.debug(f"Failed to decrypt mqtt password (using raw): {e}")

        _config_cache = {
            "host": rows.get("mqtt_republish_host", ""),
            "port": int(rows.get("mqtt_republish_port", "1883")),
            "username": rows.get("mqtt_republish_username", ""),
            "password": raw_password,
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


def get_power_connection_snapshot(*, connection=None):
    """Read current broker inputs once, without the telemetry cache or client.

    The snapshot stays in memory. Changed credentials, TLS or destination must
    be compared again by the parent before dispatch.
    """
    from contextlib import nullcontext
    from core.mqtt_connection_snapshot import MqttConnectionSnapshot
    import ipaddress
    import re
    try:
        with (nullcontext(connection) if connection is not None else engine.connect()) as conn:
            rows = {row[0]: row[1] for row in conn.execute(text(
                "SELECT key,value FROM system_config WHERE key LIKE 'mqtt_republish_%'"))}
        def boolean(value):
            if not isinstance(value, str):
                raise ValueError('Invalid boolean')
            normalized = value.strip().lower()
            if normalized in ('true', '1', 'yes', 'on'):
                return True
            if normalized in ('false', '0', 'no', 'off', ''):
                return False
            raise ValueError('Invalid boolean')
        if not boolean(rows.get('mqtt_republish_enabled', '')):
            return None
        host = rows.get('mqtt_republish_host', '')
        if not isinstance(host, str) or not host or host != host.strip() or len(host) > 253:
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            labels = host.rstrip('.').split('.')
            if any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels):
                return None
        raw_port = rows.get('mqtt_republish_port', '1883')
        if type(raw_port) is int:
            port = raw_port  # SQLite JSON affinity and native JSON scalar ports.
        elif type(raw_port) is str and re.fullmatch(r'[0-9]{1,5}', raw_port):
            port = int(raw_port)
        else:
            return None
        if not 1 <= port <= 65535:
            return None
        username = rows.get('mqtt_republish_username', '')
        password = rows.get('mqtt_republish_password', '')
        if not isinstance(username, str) or not isinstance(password, str) or '\x00' in username or '\x00' in password:
            return None
        if password:
            import core.crypto as crypto
            decoded = crypto.decrypt(password)
            # Preserve legacy plaintext, but never send an undecoded Fernet token.
            if not isinstance(decoded, str) or crypto.is_encrypted(password) and decoded == password:
                return None
            password = decoded
        return MqttConnectionSnapshot(host, port, username, password,
                                      boolean(rows.get('mqtt_republish_use_tls', '')))
    except Exception:
        log.warning('MQTT power connection settings could not be verified')
        return None


def _stop_client(client):
    """Retire a client even if either cleanup operation fails."""
    if client is None:
        return
    for operation in ("loop_stop", "disconnect"):
        try:
            getattr(client, operation)()
        except Exception:
            log.debug("MQTT client cleanup failed")


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
            except Exception as e:
                log.debug("MQTT client connection check failed")
            # Retire under the publisher lock before selecting a replacement.
            previous = _client
            _client = None
            _stop_client(previous)

        client = None
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

            # v1.8.9 (codex pass 7): runtime ITAR guard on every MQTT
            # connect — boot-audit alone misses DNS drift.
            from core.itar import enforce_host_destination
            enforce_host_destination(config["host"], scheme="mqtt")

            if client.connect(config["host"], config["port"], keepalive=60) != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError("MQTT connection refused")
            if client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError("MQTT network loop failed to start")

            _client = client
            log.info(f"Connected to external MQTT broker: {config['host']}:{config['port']}")
            return _client

        except Exception as e:
            _stop_client(client)
            log.warning("Failed to connect to external MQTT broker")
            return None


def _publish(topic_suffix: str, payload: dict):
    """Publish a message to the external broker."""
    # Event republishing is an Enterprise entitlement. The broker settings stay
    # configurable on every tier because MQTT smart-plug commands
    # (publish_power_command) use the same connection.
    from license_manager import get_license
    if not get_license().has_feature("mqtt_republish"):
        return
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



def publish_power_command(topic: str, action: str) -> str:
    """Publish exact plug command; ACK confirms broker receipt, not device state."""
    if (not isinstance(topic, str) or not topic or len(topic) > 512
            or topic != topic.strip() or any(ord(char) < 32 or ord(char) == 127 or char in "+#" for char in topic)
            or action not in ("ON", "OFF", "TOGGLE")):
        return "rejected"
    attempted = False
    try:
        client = _get_client()
        if client is None:
            return "rejected"
        # Retirement uses this same lock; bounded ACK wait cannot change clients.
        with _client_lock:
            if _client is not client:
                return "rejected"
            attempted = True
            receipt = client.publish(topic, action, qos=1, retain=False)
            if not isinstance(receipt.rc, int) or isinstance(receipt.rc, bool) or receipt.rc != 0:
                return "unconfirmed"
            receipt.wait_for_publish(timeout=5)
            return "accepted" if receipt.is_published() is True else "unconfirmed"
    except Exception:
        log.warning("MQTT plug command could not be confirmed")
        return "unconfirmed" if attempted else "rejected"

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
        previous = _client
        _client = None
        _stop_client(previous)


def test_connection(host: str, port: int, username: str = "", password: str = "",
                     use_tls: bool = False, topic_prefix: str = "odin") -> dict:
    """Test connection to an external broker. Returns {success, message}."""
    client = None
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

        # v1.8.9 (codex pass 7): ITAR guard even on test-connect.
        from core.itar import enforce_host_destination
        enforce_host_destination(host, scheme="mqtt")

        if client.connect(host, port, keepalive=10) != mqtt.MQTT_ERR_SUCCESS:
            return {"success": False, "message": "MQTT connection could not be established"}
        client.loop_start()

        # Publish a test message
        topic = f"{topic_prefix.rstrip('/')}/test"
        result = client.publish(topic, json.dumps({
            "source": "odin",
            "message": "Connection test successful",
            "timestamp": time.time(),
        }), qos=0)

        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            return {"success": False, "message": "MQTT test publication failed"}
        result.wait_for_publish(timeout=5)
        if not result.is_published():
            return {"success": False, "message": "MQTT test publication timed out"}

        return {"success": True, "message": f"Connected and published test to {topic}"}

    except Exception:
        return {"success": False, "message": "MQTT connection test failed. Check broker settings and connectivity."}
    finally:
        _stop_client(client)


def invalidate_cache():
    """Force config reload on next publish."""
    global _config_cache, _config_ts, _client
    _config_cache = None
    _config_ts = 0
    # Also disconnect so next publish reconnects with new settings
    with _client_lock:
        previous = _client
        _client = None
        _stop_client(previous)


# ---------------------------------------------------------------------------
# Event bus integration
# ---------------------------------------------------------------------------

def _on_printer_state_changed(event) -> None:
    """Republish printer telemetry when state changes."""
    data = event.data
    printer_id = data.get("printer_id")
    printer_name = data.get("printer_name", f"printer_{printer_id}")
    if printer_id is not None:
        telemetry = {k: v for k, v in data.items() if k not in ("printer_id", "printer_name")}
        republish_telemetry(printer_id, printer_name, telemetry)


def _on_job_event(event) -> None:
    """Republish job lifecycle events."""
    data = event.data
    printer_id = data.get("printer_id")
    printer_name = data.get("printer_name", f"printer_{printer_id}")
    job_event_name = event.event_type.split(".")[-1]  # "started", "completed", "failed"
    republish_job(printer_id, printer_name, job_event_name, data)


def _on_alert_dispatched(event) -> None:
    """Republish alert notifications."""
    data = event.data
    # Alert MQTT is external delivery; in-app events remain live during quiet hours.
    try:
        from sqlalchemy.orm import Session
        from modules.notifications.recipient_policy import resolve_alert_scope
        from modules.notifications.quiet_hours import should_suppress_notification
        with Session(engine) as db:
            _, org_id = resolve_alert_scope(db, printer_id=data.get("printer_id"),
                                           job_id=data.get("job_id"), spool_id=data.get("spool_id"))
        if should_suppress_notification(org_id=org_id):
            return
    except Exception:
        log.warning("MQTT alert scope or suppression check failed; delivery refused")
        return
    republish_alert(
        alert_type=data.get("alert_type", ""),
        severity=data.get("severity", "info"),
        title=data.get("title", ""),
        message=data.get("message", ""),
        printer_id=data.get("printer_id"),
        printer_name=data.get("printer_name"),
    )


def register_subscribers(bus) -> None:
    """
    Register mqtt_republish handlers on the event bus.

    Called from modules/notifications/__init__.py register_subscribers().
    """
    from core import events as ev

    bus.subscribe(ev.PRINTER_STATE_CHANGED, _on_printer_state_changed)
    bus.subscribe(ev.PRINTER_CONNECTED, _on_printer_state_changed)
    bus.subscribe(ev.PRINTER_DISCONNECTED, _on_printer_state_changed)
    bus.subscribe(ev.JOB_STARTED, _on_job_event)
    bus.subscribe(ev.JOB_COMPLETED, _on_job_event)
    bus.subscribe(ev.JOB_FAILED, _on_job_event)
    bus.subscribe("notifications.alert_dispatched", _on_alert_dispatched)
    log.debug("mqtt_republish subscribed to event bus")
