"""Maintenance and Experimental-opt-in checks at preparation and the final physical-command boundary."""
from contextlib import contextmanager
from contextvars import ContextVar
from core.printer_maintenance import hold_reason
from modules.printers.dispatch_ams_gate import GateRefusal

_context = ContextVar('maintenance_dispatch', default=None)
_refusal = ContextVar('dispatch_refusal', default=None)
_rack_start = ContextVar('rack_start_refusal', default=None)


@contextmanager
def capture_refusal():
    outcome = {}
    token = _refusal.set(outcome)
    try:
        yield outcome
    finally:
        _refusal.reset(token)


def record_code(refusal):
    outcome = _refusal.get()
    if outcome is not None:
        outcome['code'] = refusal.code


class MaintenanceHold(RuntimeError):
    pass


class ExperimentalNotEnabled(MaintenanceHold):
    """An Experimental printer's job sending was switched off before the print command. Handled as a hold."""


class RackStartRefused(MaintenanceHold):
    """The rack guard refused before MQTT publish."""


@contextmanager
def bambu_submission():
    refusal = []
    token = _rack_start.set(refusal)
    try:
        yield
    finally:
        _rack_start.reset(token)
        if refusal:
            raise RackStartRefused(refusal[0])


def record_rack_start_refusal(reason, payload):
    refusal = _rack_start.get()
    if refusal is not None and isinstance(payload, dict) and set(payload) == {'print'}:
        command = payload['print']
        if isinstance(command, dict) and command.get('command') == 'project_file':
            refusal.append(reason)


def refusal(engine, printer_id):
    with engine.connect() as conn:
        return hold_reason(conn, printer_id)


def experimental_refusal(engine, printer_id):
    from modules.printers.support_levels import experimental_refusal_for_printer
    try:
        with engine.connect() as conn:
            return experimental_refusal_for_printer(conn, printer_id)
    except Exception:
        from modules.printers.support_levels import EXPERIMENTAL_REFUSAL
        return EXPERIMENTAL_REFUSAL


def report(job, reason):
    from modules.printers.dispatch import _notify_start_outcome
    _notify_start_outcome(job, 'adapter_rejected',
        gate_refusal=GateRefusal('printer_maintenance_hold', reason), message=reason)


def report_experimental(job, reason):
    from modules.printers.dispatch import _notify_start_outcome
    from modules.printers.support_levels import EXPERIMENTAL_MESSAGE, EXPERIMENTAL_NOT_ENABLED
    prefix = f'{EXPERIMENTAL_NOT_ENABLED}: '
    message = reason[len(prefix):] if reason.startswith(prefix) else EXPERIMENTAL_MESSAGE
    _notify_start_outcome(job, 'adapter_rejected',
        gate_refusal=GateRefusal(EXPERIMENTAL_NOT_ENABLED, message), message=message)


@contextmanager
def command_context(engine, job):
    token = _context.set((engine, job))
    try:
        yield
    finally:
        _context.reset(token)


def check_command():
    context = _context.get()
    if context is not None:
        engine, job = context
        reason = refusal(engine, job['printer_id'])
        if reason:
            report(job, reason)
            raise MaintenanceHold(reason)
        reason = experimental_refusal(engine, job['printer_id'])
        if reason and reason.startswith('unsupported_rack_printer:'):
            raise RackStartRefused(reason)
        if reason:
            report_experimental(job, reason)
            raise ExperimentalNotEnabled(reason)


def start_gate(engine, printer_id):
    from modules.printers.dispatch_ams_gate import record_refusal
    def checked(*, deadline=None):
        reason = refusal(engine, printer_id)
        return record_refusal(checked, 'printer_maintenance_hold', reason) if reason else True
    checked.refusal = None
    checked.prepare = checked
    return checked
