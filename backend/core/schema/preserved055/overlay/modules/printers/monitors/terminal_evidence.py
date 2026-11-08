"""Packet evidence only: never decides the print's outcome."""
import time
from modules.printers.telemetry.validators import coerce_int_or_raise


def numeric(value):
    try:
        return coerce_int_or_raise(value)
    except ValueError:
        return None


def capture(monitor, packet, logger):
    state = packet.get('gcode_state')
    previous = getattr(monitor, '_evidence_state', None)
    if state is not None:
        monitor._evidence_state = state
    if state in ('FAILED', 'FINISH') and state != previous:
        monitor._terminal_evidence = [time.monotonic(), 0, monitor._current_job_id]
    window = getattr(monitor, '_terminal_evidence', None)
    if not window:
        return
    if state in ('RUNNING', 'PREPARE', 'PAUSE') or time.monotonic() - window[0] >= 30 or window[1] >= 20:
        monitor._terminal_evidence = None
        return
    # Strict field allowlist, numeric codes only. No names, serials or credentials.
    hms = packet.get('hms')
    codes = [{'attr': numeric(item.get('attr')), 'code': numeric(item.get('code'))}
             for item in hms if isinstance(item, dict)] if isinstance(hms, list) else None
    logger.info('Bambu terminal evidence printer_id=%s print_id=%s gcode_state=%s print_error=%s hms=%s stg_cur=%s',
                monitor.printer_id, window[2], state if state in ('FAILED', 'FINISH', 'IDLE') else None,
                numeric(packet.get('print_error')), codes, numeric(packet.get('stg_cur')))
    window[1] += 1
