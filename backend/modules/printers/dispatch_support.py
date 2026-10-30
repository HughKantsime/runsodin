"""Dispatch eligibility independent of telemetry and manual print linking."""
import re
from collections.abc import Mapping


KOBRA_DISPATCH_DETAIL = "This Kobra printer cannot dispatch through ODIN yet; start it attended and link the print to this job."


def dispatch_unsupported_reason(printer):
    """Match either stored identity, never a host address or display name."""
    def field(name):
        return printer.get(name) if isinstance(printer, Mapping) else getattr(printer, name, None)

    if is_kobra_s1(printer):
        return None
    if str(field('api_type') or '').strip().casefold() != 'moonraker':
        return None
    for name in ('model', 'machine_type'):
        identity = str(field(name) or '').casefold()
        if re.search(r'\bkobra[\s_-]*(?:s1|3)\b', identity):
            return 'printer_dispatch_unsupported: ' + KOBRA_DISPATCH_DETAIL
    return None


def is_kobra_s1(printer):
    def field(name):
        return printer.get(name) if isinstance(printer, Mapping) else getattr(printer, name, None)
    return str(field('api_type') or '').strip().casefold() == 'moonraker' and any(
        re.search(r'\bkobra[\s_-]*s1\b', str(field(name) or '').casefold())
        for name in ('model', 'machine_type'))


def automatic_dispatch_unsupported_reason(printer):
    if is_kobra_s1(printer):
        return 's1_explicit_dispatch_required: Kobra S1 requires attended explicit dispatch'
    return dispatch_unsupported_reason(printer)
