"""Publish fresh state with per-monitor throttling and MQTT connection fencing."""
import logging

from core.monitor_observations import ObservationPublisher, fresh_report

log = logging.getLogger(__name__)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def publisher(owner, printer_id):
    publishers = getattr(owner, '_live_state_publishers', None)
    if publishers is None:
        publishers = owner._live_state_publishers = {}
    if printer_id not in publishers:
        publishers[printer_id] = ObservationPublisher()
    return publishers[printer_id]


def reset_mqtt(monitor, runtime):
    """Caller holds the monitor lock; merged telemetry is never a new seed."""
    monitor._live_mqtt_state = None
    monitor._live_mqtt_generation = monitor._connection_generation
    try:
        publisher(monitor, monitor.printer_id).publish(
            runtime.get_db, monitor.printer_id, 'UNKNOWN', dbapi=True)
    except Exception:
        runtime.log.warning('[%s] Live state invalidation failed', monitor.name)


def stage_mqtt(monitor, status, raw):
    """Remember fresh explicit state for retries without writing any evidence."""
    retained = getattr(status, 'mqtt_retained', None) is True
    age = getattr(status, 'report_age_seconds', None)
    if not raw or not fresh_report(retained, age):
        return False
    explicit = raw.get('gcode_state')
    if isinstance(explicit, str) and explicit:
        monitor._live_mqtt_state = explicit
    return True


def observe_mqtt(monitor, status, raw, runtime):
    if getattr(monitor, '_live_mqtt_generation', None) != monitor._connection_generation:
        reset_mqtt(monitor, runtime)
    if not stage_mqtt(monitor, status, raw):
        return
    state = getattr(monitor, '_live_mqtt_state', None)
    if state is None:
        return
    try:
        publisher(monitor, monitor.printer_id).publish(
            runtime.get_db, monitor.printer_id, state, dbapi=True,
            report_age_seconds=getattr(status, 'report_age_seconds', None))
    except Exception:
        runtime.log.warning('[%s] Live state evidence persistence failed', monitor.name)


def observe_status(engine, printer_id, status, protocol, *, owner=None):
    raw = _mapping(getattr(status, 'raw_data', None))
    state = None
    if protocol == 'moonraker':
        state = _mapping(raw.get('print_stats')).get('state')
    elif protocol == 'prusalink':
        if _mapping(raw.get('printer')).get('state') or _mapping(raw.get('state')).get('flags'):
            state = status.internal_state
    elif protocol == 'elegoo':
        reported = _mapping(raw.get('Status') or _mapping(raw.get('Data')).get('Status'))
        if 'CurrentStatus' in reported or 'Status' in _mapping(reported.get('PrintInfo')):
            state = status.internal_state
    if state is None:
        return
    try:
        publisher(owner if owner is not None else engine, printer_id).publish(
            engine.begin, printer_id, state)
    except Exception:
        log.warning('Live state evidence persistence failed for printer %s', printer_id)
