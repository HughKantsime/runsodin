"""
Smart Plug Controller for O.D.I.N.
Supports: Tasmota (HTTP), Home Assistant (REST API), Generic MQTT
Each printer can optionally have a smart plug configured for:
- Auto power-on before print
- Auto power-off after print (with cooldown delay)
- Energy consumption tracking
- Manual on/off control
"""

import json
import math
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, Union
from dataclasses import dataclass, field

from sqlalchemy import text

# Resolved at use time: never a stale engine bound at import.
from core.db import live_engine as engine
import core.crypto as crypto
from modules.printers import plug_http_protocol

log = logging.getLogger("smart_plug")


# ---- HTTP helper (no requests dependency — use urllib) ----

def _http_request(url: str, method: str = "GET", headers: dict = None,
                  data: str = None, timeout: int = 5) -> Tuple[int, str]:
    return plug_http_protocol.http_request(url, method, headers, data, timeout)


def tasmota_power(host: str, action: str = "TOGGLE") -> Optional[bool]:
    return plug_http_protocol.tasmota_power(host, action, request=_http_request)


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
        except Exception as e:
            log.debug("Failed to parse Tasmota energy response")
    return None


# ---- Home Assistant ----

def ha_switch(ha_url: str, entity_id: str, token: str, action: str = "toggle") -> Optional[bool]:
    return plug_http_protocol.ha_switch(ha_url, entity_id, token, action,
                                       request=_http_request, readback=ha_get_state)


def ha_get_state(ha_url: str, entity_id: str, token: str) -> Optional[bool]:
    return plug_http_protocol.ha_get_state(ha_url, entity_id, token, request=_http_request)


# ---- Generic MQTT (via O.D.I.N.'s MQTT republish connection) ----

@dataclass(frozen=True)
class MqttPowerReceipt:
    status: str
    power_state: None = None


def mqtt_power(topic: str, action: str = "TOGGLE") -> MqttPowerReceipt:
    """Broker command receipt without inventing a physical power observation."""
    from modules.notifications.mqtt_republish import publish_power_command
    return MqttPowerReceipt(publish_power_command(topic, action))


# ---- Unified Interface ----

def get_plug_config(printer_id: int, *, include_storage_identity=False) -> Optional[Dict]:
    """Get smart plug config for a printer."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""SELECT plug_type, plug_host, plug_entity_id, plug_auth_token,
                      plug_auto_on, plug_auto_off, plug_cooldown_minutes, plug_power_state
               FROM printers WHERE id = :pid"""),
            {"pid": printer_id}
        ).fetchone()

    if not row or not row[0]:
        return None

    auth_token = row[3]
    if auth_token:
        try:
            auth_token = crypto.decrypt(auth_token)
        except Exception:
            pass  # Fall back to raw value if decryption fails (pre-encryption data)

    config = {
        "type": row[0],
        "host": row[1],
        "entity_id": row[2],
        "auth_token": auth_token,
        "auto_on": bool(row[4]),
        "auto_off": bool(row[5]),
        "cooldown_minutes": row[6] if row[6] is not None else 5,
        "power_state": row[7],
    }
    if include_storage_identity:
        config['_stored_auth_token'] = row[3]
    return config


def _explicit_power(printer_id, purpose, authority):
    # A bare printer ID is neither manual authority nor a physical observation.
    from modules.printers.power_authority import ManualPowerAuthority
    if type(authority) is not ManualPowerAuthority:
        return None
    config = get_plug_config(printer_id)
    if config and config['type'] in ('homeassistant', 'mqtt'):
        # These protocols cannot prove command completion. Preserve the legacy
        # process-local commands without claiming cross-process fencing.
        return {'manual_on': power_on, 'manual_off': power_off,
                'manual_toggle': power_toggle}[purpose](printer_id)
    from modules.printers.power_fence import run
    import sys
    return run(sys.modules[__name__], printer_id, purpose, authority=authority)


def power_on(printer_id: int, expected_config=None, *, authority=None) -> Optional[Union[bool, MqttPowerReceipt]]:
    """Turn on a printer's smart plug."""
    # Low-level legacy helper retained for existing dispatch/protocol callers.
    # Tasmota manual routes supply selected-request authority and automatic
    # callbacks use power_fence. HA/MQTT retain the legacy protocol path.
    if authority is not None:
        return _explicit_power(printer_id, 'manual_on', authority)
    config = get_plug_config(printer_id)
    if not config:
        return None
    if expected_config is not None and (_plug_identity(config) != expected_config or not config["auto_on"]):
        return None
    
    result = None
    if config["type"] == "tasmota":
        result = tasmota_power(config["host"], "ON")
    elif config["type"] == "homeassistant":
        result = ha_switch(config["host"], config["entity_id"], config["auth_token"], "turn_on")
    elif config["type"] == "mqtt":
        result = mqtt_power(config["entity_id"], "ON")
    
    if type(result) is bool:
        _update_power_state(printer_id, result)
    
    return result


def power_off(printer_id: int, expected_config=None, *, authority=None) -> Optional[Union[bool, MqttPowerReceipt]]:
    """Turn off a printer's smart plug."""
    # Low-level legacy helper retained for existing dispatch/protocol callers.
    # Tasmota manual routes supply selected-request authority and automatic
    # callbacks use power_fence. HA/MQTT retain the legacy protocol path.
    if authority is not None:
        return _explicit_power(printer_id, 'manual_off', authority)
    config = get_plug_config(printer_id)
    if not config:
        return None
    
    if expected_config is not None and (_plug_identity(config) != expected_config or not config["auto_off"]):
        return None

    result = None
    if config["type"] == "tasmota":
        result = tasmota_power(config["host"], "OFF")
    elif config["type"] == "homeassistant":
        result = ha_switch(config["host"], config["entity_id"], config["auth_token"], "turn_off")
    elif config["type"] == "mqtt":
        result = mqtt_power(config["entity_id"], "OFF")
    
    if type(result) is bool:
        _update_power_state(printer_id, result)
    
    return result


def power_toggle(printer_id: int, *, authority=None) -> Optional[Union[bool, MqttPowerReceipt]]:
    """Toggle a printer's smart plug."""
    # Low-level legacy helper retained for existing dispatch/protocol callers.
    # Tasmota manual routes supply selected-request authority and automatic
    # callbacks use power_fence. HA/MQTT retain the legacy protocol path.
    if authority is not None:
        return _explicit_power(printer_id, 'manual_toggle', authority)
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
    
    if type(result) is bool:
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
    
    if type(result) is bool:
        _update_power_state(printer_id, result)
    
    return result


def _update_power_state(printer_id: int, state: bool):
    """Update cached power state in database."""
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE printers SET plug_power_state = :state WHERE id = :pid"),
                         {"state": state, "pid": printer_id})
    except Exception as e:
        log.error("Failed to update plug power state")


# ---- Lifecycle Hooks ----

_cooldown_timers = {}  # Process-local; not a durable or cross-worker scheduler.
_power_locks = {}
_power_locks_guard = threading.Lock()


def _printer_power_lock(printer_id):
    with _power_locks_guard:
        return _power_locks.setdefault(printer_id, threading.RLock())


def _plug_identity(config):
    return tuple(config.get(key) for key in ("type", "host", "entity_id", "auth_token"))


@dataclass(frozen=True)
class PowerPreparation:
    accepted: bool
    required: bool
    identity: Optional[tuple] = field(default=None, repr=False)
    error: Optional[str] = None
    connection: object = field(default=None, repr=False)


def prepare_dispatch_power(printer_id: int) -> PowerPreparation:
    """Cancel pending shutdown and request guarded ON, not printer readiness.

    Caller must already authorize the job and must independently verify fresh
    printer readiness and revalidate authority/configuration before uploading.
    """
    with _printer_power_lock(printer_id):
        timer = _cooldown_timers.pop(printer_id, None)
        if timer is not None:
            timer.cancel()
        try:
            config = get_plug_config(printer_id)
            if not config or not config["auto_on"]:
                return PowerPreparation(accepted=True, required=False)
            identity = _plug_identity(config)
            result = power_on(printer_id, expected_config=identity)
            current = get_plug_config(printer_id)
            if not current or not current["auto_on"] or _plug_identity(current) != identity:
                return PowerPreparation(False, True, error="Plug configuration changed; check before retrying")
            if result is True or isinstance(result, MqttPowerReceipt) and result.status == "accepted":
                return PowerPreparation(True, True, identity=identity)
            return PowerPreparation(False, True, error="Power-on was not confirmed; check plug state before retrying")
        except Exception:
            log.warning("Dispatch power preparation could not be confirmed")
            return PowerPreparation(False, True, error="Power-on was not confirmed; check plug state before retrying")


def _conditional_power_observation(printer_id, state, config, *, enable_flag="auto_on"):
    """Persist a measured boolean only against the exact stored plug identity."""
    if type(state) is not bool or '_stored_auth_token' not in config:
        return False
    try:
        with engine.begin() as conn:
            flag_clause = (" AND plug_auto_on=:enabled" if enable_flag == 'auto_on' else
                           " AND plug_auto_off=:enabled" if enable_flag == 'auto_off' else "")
            result = conn.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe: internal constant SQL fragments only; all values bound
                f"""
                UPDATE printers SET plug_power_state=:state
                WHERE id=:pid {flag_clause}
                AND (plug_type=CAST(:kind AS VARCHAR) OR (plug_type IS NULL AND CAST(:kind AS VARCHAR) IS NULL))
                AND (plug_host=CAST(:host AS VARCHAR) OR (plug_host IS NULL AND CAST(:host AS VARCHAR) IS NULL))
                AND (plug_entity_id=CAST(:entity AS VARCHAR) OR (plug_entity_id IS NULL AND CAST(:entity AS VARCHAR) IS NULL))
                AND (plug_auth_token=CAST(:token AS VARCHAR) OR (plug_auth_token IS NULL AND CAST(:token AS VARCHAR) IS NULL))
            """), {'pid': printer_id, 'state': state, 'enabled': True,
                   'kind': config['type'], 'host': config['host'],
                   'entity': config['entity_id'], 'token': config['_stored_auth_token']})
            return result.rowcount == 1
    except Exception:
        log.warning('Power observation could not be stored')
        return False


def prepare_bounded_dispatch_power(printer_id, deadline):
    """Own the pending-off lock through one bounded command and cleanup.

    Database reads/writes have their engine's timeout, not a hard process
    bound. Caller must perform final authority/configuration/readiness checks.
    """
    import math
    from modules.printers.plug_power_acquisition import bounded_power_on
    failure = PowerPreparation(False, True, error='Power-on was not confirmed; check plug state before retrying')
    lock = None
    acquired = False
    try:
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            return failure
        deadline = float(deadline)
        remaining = deadline - time.monotonic()
        if not math.isfinite(deadline) or not 0 < remaining <= threading.TIMEOUT_MAX:
            return failure
        lock = _printer_power_lock(printer_id)
        acquired = lock.acquire(timeout=remaining)
        if not acquired:
            return failure
        timer = _cooldown_timers.pop(printer_id, None)
        if timer is not None:
            timer.cancel()
        config = get_plug_config(printer_id, include_storage_identity=True)
        if time.monotonic() >= deadline:
            return failure
        if not config or not config['auto_on']:
            return PowerPreparation(True, False)
        identity = _plug_identity(config)
        connection = None
        if config['type'] == 'mqtt':
            from modules.notifications.mqtt_republish import get_power_connection_snapshot
            connection = get_power_connection_snapshot()
            if connection is None:
                return failure
        attempt = bounded_power_on(config, deadline, connection)
        current = get_plug_config(printer_id, include_storage_identity=True)
        if (not current or not current['auto_on'] or _plug_identity(current) != identity
                or current['_stored_auth_token'] != config['_stored_auth_token']):
            return PowerPreparation(False, True, error='Plug configuration changed; check before retrying')
        if config['type'] == 'mqtt' and get_power_connection_snapshot() != connection:
            return PowerPreparation(False, True, error='Broker configuration changed; check before retrying')
        if time.monotonic() >= deadline or attempt.status != 'accepted':
            return failure
        if type(attempt.power_state) is bool:
            if attempt.power_state is not True or not _conditional_power_observation(printer_id, attempt.power_state, config):
                return failure
        elif attempt.power_state is not None or config['type'] != 'mqtt':
            return failure
        if time.monotonic() >= deadline:
            return failure
        return PowerPreparation(True, True, identity=identity, connection=connection)
    except Exception:
        log.warning('Bounded dispatch power preparation could not be confirmed')
        return failure
    finally:
        if acquired:
            lock.release()


def _auto_off_safe(printer_id):
    """Refuse unknown/stale telemetry or an active persisted print."""
    with engine.connect() as conn:
        row = conn.execute(text("SELECT gcode_state,last_seen FROM printers WHERE id=:pid"), {"pid": printer_id}).fetchone()
        if not row or row[0] not in ("IDLE", "FINISH") or not row[1]:
            return False
        stamp = row[1] if isinstance(row[1], datetime) else datetime.fromisoformat(row[1].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        clock = conn.execute(text('SELECT clock_timestamp()' if conn.dialect.name == 'postgresql' else "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")).scalar_one()
        from modules.printers.power_fence import _utc
        age = (_utc(clock) - stamp).total_seconds()
        if not 0 <= age <= 90:
            return False
        active = conn.execute(text("SELECT 1 FROM print_jobs WHERE printer_id=:pid AND status='running' LIMIT 1"), {"pid": printer_id}).first()
        queued = conn.execute(text("SELECT 1 FROM jobs WHERE printer_id=:pid AND status IN ('printing','paused') LIMIT 1"), {"pid": printer_id}).first()
        claimed = conn.execute(text("SELECT 1 FROM printer_dispatch_claims WHERE printer_id=:pid LIMIT 1"), {"pid": printer_id}).first()
        return active is None and queued is None and claimed is None


# HA/MQTT cannot prove remote completion. These callbacks deliberately retain
# origin/main process-local locking and command behavior, without durable fences.
def _unfenced_on_print_start(printer_id: int, *, print_job_id=None):
    """Cancel pending off for every start; auto-on remains an observed-start hook."""
    with _printer_power_lock(printer_id):
        timer = _cooldown_timers.pop(printer_id, None)
        if timer is not None:
            timer.cancel()
        if print_job_id is not None:
            from core.auth import SECRET_KEY
            from core.dispatch_observations import bind_generic_observation
            key=SECRET_KEY.encode() if isinstance(SECRET_KEY,str) else None
            outcome=bind_generic_observation(engine,printer_id=printer_id,print_job_id=print_job_id,key=key)
            if outcome!='no_claim':
                return
        config = get_plug_config(printer_id)
        if config and config["auto_on"]:
            # A retained dispatch attempt already owns power preparation. Never
            # replay ON from telemetry, including uncertain/crashed attempts.
            # Exact observation binding is a separate durable receipt contract.
            with engine.connect() as conn:
                claimed = conn.execute(text(
                    "SELECT 1 FROM printer_dispatch_claims WHERE printer_id=:pid LIMIT 1"
                ), {"pid": printer_id}).first()
            if claimed is not None:
                return
            power_on(printer_id)


def _unfenced_on_print_complete(printer_id: int):
    """Schedule an identity-guarded off; cancellation cannot undo sent commands."""
    with _printer_power_lock(printer_id):
        previous = _cooldown_timers.pop(printer_id, None)
        if previous is not None:
            previous.cancel()
        config = get_plug_config(printer_id)
        if not config or not config["auto_off"]:
            return
        minutes = config["cooldown_minutes"]
        if type(minutes) is not int or not 0 <= minutes <= 1440:
            log.warning("Automatic power-off skipped: invalid cooldown")
            return
        identity = _plug_identity(config)

        def delayed_off():
            with _printer_power_lock(printer_id):
                if _cooldown_timers.get(printer_id) is not timer:
                    return
                try:
                    current = get_plug_config(printer_id)
                    if (current and current["auto_off"] and _plug_identity(current) == identity
                            and _auto_off_safe(printer_id)):
                        power_off(printer_id, expected_config=identity)
                except Exception:
                    log.warning("Automatic power-off could not be completed")
                finally:
                    if _cooldown_timers.get(printer_id) is timer:
                        del _cooldown_timers[printer_id]

        timer = threading.Timer(minutes * 60, delayed_off)
        timer.daemon = True
        _cooldown_timers[printer_id] = timer
        try:
            timer.start()
        except Exception:
            if _cooldown_timers.get(printer_id) is timer:
                del _cooldown_timers[printer_id]
            timer.cancel()
            log.warning("Automatic power-off timer could not start")


def on_print_start(printer_id: int, *, print_job_id=None):
    """Route legacy protocols separately from target-first Tasmota admission."""
    config = get_plug_config(printer_id)
    if config and config["type"] in ("homeassistant", "mqtt"):
        return _unfenced_on_print_start(printer_id, print_job_id=print_job_id)
    # Cancel first under the local lock; target acquisition never nests inside it.
    with _printer_power_lock(printer_id):
        timer = _cooldown_timers.pop(printer_id, None)
        if timer is not None:
            timer.cancel()
    if type(print_job_id) is not int or print_job_id <= 0:
        return
    from core.auth import SECRET_KEY
    from core.dispatch_observations import bind_generic_observation
    key = SECRET_KEY.encode() if isinstance(SECRET_KEY, str) else SECRET_KEY
    if bind_generic_observation(engine, printer_id=printer_id, print_job_id=print_job_id, key=key) != 'no_claim':
        return
    from modules.printers.power_fence import run
    import sys
    run(sys.modules[__name__], printer_id, 'observed_on', physical_id=print_job_id)


def on_print_complete(printer_id: int, *, print_job_id=None):
    """Keep legacy timers; Tasmota OFF requires physical observation authority."""
    config = get_plug_config(printer_id)
    if config and config["type"] in ("homeassistant", "mqtt"):
        return _unfenced_on_print_complete(printer_id)
    # For Tasmota, bare IDs schedule inert metadata and never authorize OFF.
    from modules.printers.power_fence import run, _snapshot
    import sys
    module = sys.modules[__name__]
    # No target acquisition while scheduling under the process lock.
    try:
        expected = _snapshot(module, printer_id, 'cooldown_off', print_job_id, admission=False) if print_job_id is not None else None
    except Exception:
        expected = None
    with _printer_power_lock(printer_id):
        previous = _cooldown_timers.pop(printer_id, None)
        if previous is not None:
            previous.cancel()
        # This is scheduling metadata only; the fenced callback refreshes
        # the complete configuration and authority before any command.
        if not config or not config['auto_off']:
            return
        minutes = config['cooldown_minutes']
        if type(minutes) is not int or not 0 <= minutes <= 1440:
            log.warning('Automatic power-off skipped: invalid cooldown')
            return
        def delayed_off():
            try:
                if expected is not None:
                    run(module, printer_id, 'cooldown_off', physical_id=print_job_id,
                        expected=expected, timer=timer)
            except Exception:
                log.warning('Automatic power-off could not be completed')
            finally:
                with _printer_power_lock(printer_id):
                    if _cooldown_timers.get(printer_id) is timer:
                        del _cooldown_timers[printer_id]
        timer = threading.Timer(minutes * 60, delayed_off)
        timer.daemon = True
        _cooldown_timers[printer_id] = timer
    # An immediate timer must also enter target-first, outside the local lock.
    try:
        timer.start()
    except Exception:
        with _printer_power_lock(printer_id):
            if _cooldown_timers.get(printer_id) is timer:
                del _cooldown_timers[printer_id]
        timer.cancel()
        log.warning('Automatic power-off timer could not start')


# Per-print energy: cumulative plug counter read at start and end of a print.
# Start readings are process-local; a restart mid-print leaves that print unknown.
_ENERGY_START_LIMIT = 512
_ENERGY_APPLY_ATTEMPTS = 10
_ENERGY_APPLY_INTERVAL = 1.0
_energy_starts: Dict[int, tuple] = {}
_energy_ended: Dict[int, float] = {}  # prints that ended before their delayed start reading
_energy_lock = threading.Lock()


def _energy_counter(printer_id: int) -> Optional[float]:
    try:
        energy = get_energy(printer_id)
    except Exception:
        return None
    value = energy.get("total_kwh") if isinstance(energy, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def record_energy_start(printer_id: int, print_job_id: int) -> None:
    reading = _energy_counter(printer_id)
    if reading is None:
        return
    with _energy_lock:
        if _energy_ended.pop(print_job_id, None) is not None:
            return
        if len(_energy_starts) >= _ENERGY_START_LIMIT:
            _energy_starts.pop(next(iter(_energy_starts)))
        _energy_starts[print_job_id] = (printer_id, reading)


def record_energy_for_print(printer_id: int, print_job_id: int) -> Optional[float]:
    """Measure consumption since the print's start reading and store it on its archive.

    Returns the consumed kWh that will be written, or None when unknown (no start
    reading, no current reading, or a counter reset).
    """
    with _energy_lock:
        start = _energy_starts.pop(print_job_id, None)
        if start is None:
            if len(_energy_ended) >= _ENERGY_START_LIMIT:
                _energy_ended.pop(next(iter(_energy_ended)))
            _energy_ended[print_job_id] = time.monotonic()
    if start is None or start[0] != printer_id:
        return None
    end_kwh = _energy_counter(printer_id)
    if end_kwh is None or end_kwh < start[1]:
        return None
    consumed = round(end_kwh - start[1], 4)
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM system_config WHERE key = 'energy_cost_per_kwh'")).fetchone()
        rate = float(row[0]) if row else 0.12
        if not math.isfinite(rate) or rate < 0:
            rate = None
    except Exception:
        rate = None
    cost = round(consumed * rate, 4) if rate is not None else None
    _apply_energy(printer_id, print_job_id, consumed, cost, _ENERGY_APPLY_ATTEMPTS)
    return consumed


def _apply_energy(printer_id, print_job_id, consumed, cost, attempts):
    """Write once to the print's archive; retry while the archive row does not exist yet."""
    try:
        with engine.begin() as conn:
            exists = conn.execute(text("SELECT 1 FROM print_archives WHERE print_job_id = :pj LIMIT 1"),
                                  {"pj": print_job_id}).first()
            if exists is None:
                applied = None
            else:
                applied = conn.execute(text(
                    "UPDATE print_archives SET energy_kwh = :kwh, energy_cost = :cost "
                    "WHERE print_job_id = :pj AND energy_kwh IS NULL"),
                    {"kwh": consumed, "cost": cost, "pj": print_job_id}).rowcount
                if applied:
                    conn.execute(text("UPDATE printers SET plug_energy_kwh = COALESCE(plug_energy_kwh, 0) + :kwh WHERE id = :pid"),
                                 {"kwh": consumed, "pid": printer_id})
    except Exception:
        log.warning("Failed to record plug energy")
        return
    if applied is None and attempts <= 1:
        log.warning("Plug energy measured but no print archive appeared to store it")
    if applied is None and attempts > 1:
        timer = threading.Timer(_ENERGY_APPLY_INTERVAL, _apply_energy,
                                (printer_id, print_job_id, consumed, cost, attempts - 1))
        timer.daemon = True
        timer.start()


def _in_background(function, *args):
    worker = threading.Thread(target=function, args=args, daemon=True)
    worker.start()
    return worker


# ---------------------------------------------------------------------------
# Event bus integration
# ---------------------------------------------------------------------------

def _on_job_started_event(event) -> None:
    """Handle job.started event — energy start reading, auto power-on if configured."""
    _on_energy_start_event(event)
    printer_id = event.data.get("printer_id")
    if printer_id is not None:
        try:
            on_print_start(printer_id, print_job_id=event.data.get("print_job_id"))
        except Exception as e:
            log.warning("Smart plug start hook failed")


_ENERGY_START_DELAY = 2.0


def _on_energy_start_event(event) -> None:
    """Read the start counter shortly after the publisher's start transaction commits.

    JOB_STARTED is published from inside monitor transactions; an immediate
    concurrent read of plug configuration could contend with that writer on
    non-WAL SQLite. The delay costs at most a couple of seconds of metering.
    """
    printer_id = event.data.get("printer_id")
    print_job_id = event.data.get("print_job_id")
    if type(printer_id) is int and type(print_job_id) is int:
        _in_background(_delayed_energy_start, printer_id, print_job_id)


def _delayed_energy_start(printer_id, print_job_id):
    time.sleep(_ENERGY_START_DELAY)
    record_energy_start(printer_id, print_job_id)


def _on_energy_end_event(event) -> None:
    """Completed, failed and cancelled prints all consumed energy."""
    printer_id = event.data.get("printer_id")
    print_job_id = event.data.get("print_job_id")
    if type(printer_id) is int and type(print_job_id) is int:
        _in_background(record_energy_for_print, printer_id, print_job_id)


def _on_job_completed_event(event) -> None:
    """Handle job.completed / job.failed event — energy reading, auto power-off if configured."""
    _on_energy_end_event(event)
    printer_id = event.data.get("printer_id")
    success = event.data.get("success", True)
    if printer_id is not None and success:
        try:
            on_print_complete(printer_id, print_job_id=event.data.get("print_job_id"))
        except Exception as e:
            log.warning("Smart plug completion hook failed")


def register_subscribers(bus) -> None:
    """
    Register smart plug handlers on the event bus.

    Called from modules/printers/__init__.py register_subscribers().
    """
    from core import events as ev

    bus.subscribe(ev.JOB_STARTED, _on_job_started_event)
    bus.subscribe(ev.JOB_COMPLETED, _on_job_completed_event)
    bus.subscribe(ev.JOB_FAILED, _on_energy_end_event)
    bus.subscribe(ev.JOB_CANCELLED, _on_energy_end_event)
    log.debug("smart_plug subscribed to event bus")
