"""Owned HTTP plug ON acquisition; no database or publisher imports.

This helper is not dispatch-wired. A timeout after worker start is uncertain:
no retry is performed. Normal exit and an on observation before the deadline
are required for HTTP acceptance. MQTT acknowledgment has unknown physical state.
"""
from dataclasses import dataclass
import math
import threading
import time
from modules.printers.dispatch_acquisition import _spawn_context, _cleanup_owned_process


@dataclass(frozen=True)
class PowerAttempt:
    status: str
    power_state: object = None


def _http_power_worker(sender, config, deadline):
    try:
        from modules.printers import plug_http_protocol as protocol
        def request(*args, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 0, 'Power request deadline elapsed'
            kwargs['timeout'] = remaining
            return protocol.http_request(*args, **kwargs)
        if config['type'] == 'tasmota':
            observed = protocol.tasmota_power(config['host'], 'ON', request=request)
        else:
            observed = protocol.ha_switch(config['host'], config['entity_id'],
                                          config['auth_token'], 'turn_on', request=request)
        sender.send(('observed', observed) if type(observed) is bool else ('uncertain', None))
    except Exception:
        try:
            sender.send(('uncertain', None))
        except Exception:
            pass
    finally:
        sender.close()


def _mqtt_power_worker(sender, config, deadline):
    try:
        from core.mqtt_power_protocol import send_owned_mqtt_on
        sender.send(('broker', send_owned_mqtt_on(config['connection'], config['entity_id'], deadline)))
    except Exception:
        try:
            sender.send(('broker', 'uncertain'))
        except Exception:
            pass
    finally:
        sender.close()


def bounded_http_power_on(config: dict, deadline: float) -> PowerAttempt:
    if not isinstance(config, dict) or config.get('type') not in ('tasmota', 'homeassistant'):
        return PowerAttempt('refused')
    return bounded_power_on(config, deadline)


def bounded_power_on(config: dict, deadline: float, connection=None) -> PowerAttempt:
    """Send one ON at most, reap our worker, and never accept a late result."""
    receiver = sender = process = None
    result = PowerAttempt('refused')
    try:
        if not isinstance(config, dict) or config.get('type') not in ('tasmota', 'homeassistant', 'mqtt'):
            return result
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            return result
        deadline = float(deadline)
        remaining = deadline - time.monotonic()
        if not math.isfinite(deadline) or not 0 < remaining <= threading.TIMEOUT_MAX:
            return result
        worker = _http_power_worker
        if config['type'] == 'mqtt':
            from core.mqtt_connection_snapshot import MqttConnectionSnapshot
            from core.mqtt_power_protocol import valid_power_topic
            if not isinstance(connection, MqttConnectionSnapshot) or not valid_power_topic(config.get('entity_id')):
                return result
            if (not isinstance(connection.host, str) or not connection.host
                    or type(connection.port) is not int or not 1 <= connection.port <= 65535
                    or type(connection.use_tls) is not bool
                    or not isinstance(connection.username, str) or not isinstance(connection.password, str)):
                return result
            inputs = {'type': 'mqtt', 'entity_id': config['entity_id'], 'connection': connection}
            worker = _mqtt_power_worker
        else:
            keys = ('type', 'host') if config['type'] == 'tasmota' else ('type', 'host', 'entity_id', 'auth_token')
            inputs = {key: config[key] for key in keys}
            if any(not isinstance(value, str) for value in inputs.values()) or not inputs['host']:
                return result
        context = _spawn_context()
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=worker, args=(sender, inputs, deadline), daemon=True)
        # A failed start might have spawned a command worker. Refuse to retry.
        result = PowerAttempt('uncertain')
        process.start()
        sender.close()
        remaining = deadline - time.monotonic()
        if remaining > 0 and receiver.poll(remaining):
            receipt = receiver.recv()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                process.join(timeout=remaining)
                normal = process.exitcode == 0 and not process.is_alive() and time.monotonic() < deadline
                if config['type'] != 'mqtt' and normal and type(receipt) is tuple and len(receipt) == 2 and receipt[0] == 'observed' and type(receipt[1]) is bool:
                    result = PowerAttempt('accepted' if receipt[1] else 'refused', receipt[1])
                elif normal and type(receipt) is tuple and len(receipt) == 2 and receipt[0] == 'broker' and receipt[1] in ('accepted', 'refused', 'uncertain') and config['type'] == 'mqtt':
                    result = PowerAttempt(receipt[1])
    except Exception:
        pass
    finally:
        for end in (receiver, sender):
            if end is not None:
                try:
                    end.close()
                except Exception:
                    result = PowerAttempt('uncertain')
        if process is not None and not _cleanup_owned_process(process):
            result = PowerAttempt('uncertain')
    return result
