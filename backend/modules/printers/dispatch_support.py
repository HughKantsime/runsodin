"""Dispatch eligibility independent of telemetry and manual print linking."""
import re
from collections.abc import Mapping


S1_BED_CLEAR_REQUIRED = 's1_bed_clear_required: Start Kobra S1 prints with Start next and confirm the bed is clear'
KOBRA_DISPATCH_DETAIL = "This Kobra printer cannot dispatch through ODIN yet; start it attended and link the print to this job."


def dispatch_unsupported_reason(printer):
    """Match either stored identity, never a host address or display name."""
    def field(name):
        return printer.get(name) if isinstance(printer, Mapping) else getattr(printer, name, None)

    if is_kobra_s1(printer):
        if s1_sending_enabled():
            return None
        return 'printer_dispatch_unsupported: ' + S1_SENDING_OFF_DETAIL
    if str(field('api_type') or '').strip().casefold() != 'moonraker':
        return None
    for name in ('model', 'machine_type'):
        identity = str(field(name) or '').casefold()
        if re.search(r'\bkobra[\s_-]*(?:s1|3)(?![a-z0-9])', identity):
            return 'printer_dispatch_unsupported: ' + KOBRA_DISPATCH_DETAIL
    return None


S1_SENDING_OFF_DETAIL = ("Kobra S1 sending is experimental and is turned off on this server; start it attended and "
                         "link the print to this job. An administrator can turn it on with ODIN_KOBRA_S1_SENDING=1.")


def s1_sending_enabled():
    """Experimental, off unless the server operator opts in. Read at call time, never cached."""
    import os
    return os.environ.get('ODIN_KOBRA_S1_SENDING', '').strip().lower() in ('1', 'true', 'yes')


def is_kobra_s1(printer):
    def field(name):
        return printer.get(name) if isinstance(printer, Mapping) else getattr(printer, name, None)
    return str(field('api_type') or '').strip().casefold() == 'moonraker' and any(
        re.search(r'\bkobra[\s_-]*s1(?![a-z0-9])(?![\s_-]*max(?![a-z0-9]))', str(field(name) or '').casefold())
        for name in ('model', 'machine_type'))


def automatic_dispatch_unsupported_reason(printer):
    if is_kobra_s1(printer) and s1_sending_enabled():
        return 's1_explicit_dispatch_required: Kobra S1 requires attended explicit dispatch'
    return dispatch_unsupported_reason(printer)
