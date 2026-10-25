"""Owned bounded plug acquisition; no database or publisher imports.

Existing dispatch ON semantics are preserved. The separate fenced action receipt
requires normal exit, owned cleanup and exact reported relay state before deadline.
A timeout after worker start is uncertain; no retry is performed. MQTT PUBACK
has unknown physical state and is not a fenced-command completion receipt.
"""
from dataclasses import dataclass, field
import math
import threading
import time
from modules.printers.dispatch_acquisition import _spawn_context, _cleanup_owned_process


@dataclass(frozen=True)
class PowerAttempt:
    status: str
    power_state: object = None
    normal_exit: bool = field(default=False, compare=False)
    reaped: bool = field(default=False, compare=False)
    ipc_closed: bool = field(default=False, compare=False)
    start_uncertain: bool = field(default=False, compare=False)


# Strong references survive callback return when owned cleanup is unconfirmed.
# This is process-local bookkeeping, never restart recovery authority.
_uncertain_resources = []
_RECEIPT_SEAL = object()


@dataclass(frozen=True)
class _OwnedHttpResult:
    attempt: PowerAttempt
    seal: object = field(repr=False)

    def confirmed(self, expected):
        a = self.attempt
        return (self.seal is _RECEIPT_SEAL and a.status == 'accepted'
                and type(a.power_state) is bool and a.power_state is expected
                and a.normal_exit and a.reaped and a.ipc_closed and not a.start_uncertain)


def bounded_http_power_action(config, deadline, action):
    if not isinstance(config, dict) or action not in ('ON', 'OFF', 'TOGGLE') or config.get('type') not in ('tasmota', 'homeassistant'):
        return _OwnedHttpResult(PowerAttempt('refused'), _RECEIPT_SEAL)
    return _OwnedHttpResult(bounded_power_on(config, deadline, _action=action), _RECEIPT_SEAL)


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
            observed = protocol.tasmota_power(config['host'], config.get('_action', 'ON'), request=request)
        else:
            observed = protocol.ha_switch(config['host'], config['entity_id'],
                                          config['auth_token'], {'ON': 'turn_on', 'OFF': 'turn_off', 'TOGGLE': 'toggle'}[config.get('_action', 'ON')], request=request)
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


def bounded_power_on(config: dict, deadline: float, connection=None, *, _action='ON') -> PowerAttempt:
    """Send one ON at most, reap our worker, and never accept a late result."""
    receiver = sender = process = None
    result = PowerAttempt('refused')
    normal = False
    started = False
    ipc_closed = True
    reaped = False
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
        if _action not in ('ON', 'OFF', 'TOGGLE') or (_action != 'ON' and config['type'] == 'mqtt'):
            return result
        if _action != 'ON':
            inputs['_action'] = _action
        context = _spawn_context()
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=worker, args=(sender, inputs, deadline), daemon=True)
        # A failed start might have spawned a command worker. Refuse to retry.
        result = PowerAttempt('uncertain')
        process.start()
        started = True
        sender.close()
        remaining = deadline - time.monotonic()
        if remaining > 0 and receiver.poll(remaining):
            receipt = receiver.recv()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                process.join(timeout=remaining)
                normal = process.exitcode == 0 and not process.is_alive() and time.monotonic() < deadline
                if config['type'] != 'mqtt' and normal and type(receipt) is tuple and len(receipt) == 2 and receipt[0] == 'observed' and type(receipt[1]) is bool:
                    result = PowerAttempt('accepted' if (_action == 'TOGGLE' or receipt[1] is (_action == 'ON')) else 'refused', receipt[1])
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
                    ipc_closed = False
                    result = PowerAttempt('uncertain')
        if process is not None:
            reaped = _cleanup_owned_process(process)
            if not reaped or not ipc_closed:
                _uncertain_resources.append((process, receiver, sender))
                result = PowerAttempt('uncertain')
    return PowerAttempt(result.status, result.power_state, normal, reaped,
                        ipc_closed, process is not None and not started)
