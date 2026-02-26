"""
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

from db_utils import get_db
import crypto

log = logging.getLogger("smart_plug")


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
        except Exception:
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
        except Exception:
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
        except Exception:
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
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT plug_type, plug_host, plug_entity_id, plug_auth_token,
                      plug_auto_on, plug_auto_off, plug_cooldown_minutes, plug_power_state
               FROM printers WHERE id = ?""",
            (printer_id,)
        )
        row = cur.fetchone()

    if not row or not row[0]:
        return None

    auth_token = row[3]
    if auth_token:
        try:
            auth_token = crypto.decrypt(auth_token)
        except Exception:
            pass  # Fall back to raw value if decryption fails (pre-encryption data)

    return {
        "type": row[0],
        "host": row[1],
        "entity_id": row[2],
        "auth_token": auth_token,
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
        # Need the raw state value (a number), not ha_get_state which returns bool
        url = f"{config['host'].rstrip('/')}/api/states/{energy_entity}"
        headers = {"Authorization": f"Bearer {config['auth_token']}"}
        status_code, body = _http_request(url, headers=headers)
        if status_code == 200:
            try:
                data = json.loads(body)
                return {"total_kwh": float(data.get("state", 0)), "power_w": 0, "voltage": 0, "current": 0}
            except (ValueError, TypeError):
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
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE printers SET plug_power_state = ? WHERE id = ?", (state, printer_id))
            conn.commit()
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
        with get_db() as conn:
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
            log.info(f"Energy recorded for job {job_id}: {consumed:.4f} kWh (${cost:.4f})")
    except Exception as e:
        log.error(f"Failed to record energy: {e}")
