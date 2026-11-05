"""Bound one live HMS report to its print and connection while awaiting state."""
from copy import deepcopy

from core.monitor_observations import fresh_report

STOPPED_STATES = frozenset(('FAILED', 'PAUSE', 'IDLE'))


def prepare(monitor, raw, status, now):
    retained = getattr(status, 'mqtt_retained', None) is True
    age = getattr(status, 'report_age_seconds', None)
    packet = dict(raw, _hms_retained=retained, _hms_report_age=age, _hms_received_at=now)
    job_id = monitor._current_job_id
    generation = monitor._connection_generation
    pending = getattr(monitor, '_hms_corroboration', None)
    name = raw.get('subtask_name') or raw.get('gcode_file')
    if pending and (pending['job_id'] != job_id or pending['generation'] != generation
                    or not 0 <= now - pending['at'] <= 60 or (name and name != pending['name'])):
        pending = None
        monitor._hms_corroboration = None
    if not fresh_report(retained, age):
        return packet
    if 'hms' in raw and isinstance(raw['hms'], list):
        monitor._hms_corroboration = None
        if raw['hms'] and name and job_id is not None and raw.get('gcode_state') not in STOPPED_STATES:
            monitor._hms_corroboration = {
                'packet': deepcopy(packet), 'name': name, 'job_id': job_id,
                'generation': generation, 'at': now - (age or 0),
            }
    elif pending and raw.get('gcode_state') in STOPPED_STATES:
        packet = dict(pending['packet'], gcode_state=raw['gcode_state'])
        monitor._hms_corroboration = None
    return packet


def evidence(packet, now):
    age = packet.get('_hms_report_age')
    received = packet.get('_hms_received_at')
    if received is not None and (age is None or type(age) in (int, float)):
        age = (age or 0) + now - received
    return {
        'mqtt_retained': packet.get('_hms_retained', False),
        'report_age_seconds': age,
        'live_state': packet.get('gcode_state'),
    }


def corroborate_retry(packet, raw, status, now):
    """A DB retry can receive its first stopped-state evidence in a later report."""
    previous = evidence(packet, now)
    name = raw.get('subtask_name') or raw.get('gcode_file')
    original_name = packet.get('subtask_name') or packet.get('gcode_file')
    if (fresh_report(previous['mqtt_retained'], previous['report_age_seconds'])
            and fresh_report(getattr(status, 'mqtt_retained', False), getattr(status, 'report_age_seconds', None))
            and raw.get('gcode_state') in STOPPED_STATES and (not name or name == original_name)):
        return dict(packet, gcode_state=raw['gcode_state'])
    return packet
