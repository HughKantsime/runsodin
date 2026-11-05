"""Current pause text using existing decoders, independent of historical errors."""
from modules.printers.hms_codes import lookup_print_error
from modules.notifications.event_dispatcher import parse_hms_errors


def observe(monitor, packet):
    if not isinstance(packet.get('hms'), list):
        return
    active = [item for item in packet['hms'] if isinstance(item, dict)
              and type(item.get('attr')) is int and type(item.get('code')) is int]
    prior = getattr(monitor, '_pause_active_hms', [])
    # Keep arrival order among still-active codes; new arrivals are most recent.
    monitor._pause_active_hms = [item for item in prior if item in active] + [item for item in active if item not in prior]


def stage(monitor, fallback):
    if monitor._state.get('gcode_state') != 'PAUSE':
        return fallback
    try:
        error = int(monitor._state.get('print_error') or 0)
    except (TypeError, ValueError):
        error = 0
    if error:
        return f'Paused: print_error={error:08X}'
    errors = parse_hms_errors(getattr(monitor, '_pause_active_hms', []))
    return 'Paused: hms=' + errors[-1]['code'] if errors else 'Paused: Cause unavailable'


def display_stage(value):
    """Decode compact storage (print_stage is VARCHAR(50)) at the API boundary."""
    from modules.printers.hms_codes import lookup_hms_code
    if isinstance(value, str) and value.startswith('Paused: print_error='):
        code = value.removeprefix('Paused: print_error=')
        return 'Paused: ' + lookup_print_error(code[:4] + '_' + code[4:])
    if isinstance(value, str) and value.startswith('Paused: hms='):
        return 'Paused: ' + lookup_hms_code(value.removeprefix('Paused: hms='))
    return value
