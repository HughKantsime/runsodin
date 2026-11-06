"""Maintenance checks at preparation and the final physical-command boundary."""
from contextlib import contextmanager
from contextvars import ContextVar
from core.printer_maintenance import hold_reason
from modules.printers.dispatch_ams_gate import GateRefusal

_context = ContextVar('maintenance_dispatch', default=None)
_refusal = ContextVar('dispatch_refusal', default=None)


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


def refusal(engine, printer_id):
    with engine.connect() as conn:
        return hold_reason(conn, printer_id)


def report(job, reason):
    from modules.printers.dispatch import _notify_start_outcome
    _notify_start_outcome(job, 'adapter_rejected',
        gate_refusal=GateRefusal('printer_maintenance_hold', reason), message=reason)


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


def start_gate(engine, printer_id):
    from modules.printers.dispatch_ams_gate import record_refusal
    def checked(*, deadline=None):
        reason = refusal(engine, printer_id)
        return record_refusal(checked, 'printer_maintenance_hold', reason) if reason else True
    checked.refusal = None
    checked.prepare = checked
    return checked
