"""Dispatch eligibility independent of telemetry and manual print linking."""
import re
from collections.abc import Mapping


KOBRA_DISPATCH_DETAIL = "Kobra S1 dispatch through ODIN isn't available yet; start it attended and link the print to this job."


def dispatch_unsupported_reason(printer):
    """Match either stored identity, never a host address or display name."""
    def field(name):
        return printer.get(name) if isinstance(printer, Mapping) else getattr(printer, name, None)

    api_type = str(field('api_type') or '').strip().casefold()
    from modules.printers.support_levels import MONITOR_ONLY, MONITOR_ONLY_DETAIL
    if api_type in MONITOR_ONLY:
        return 'printer_dispatch_unsupported: ' + MONITOR_ONLY_DETAIL
    if api_type != 'moonraker':
        return None
    for name in ('model', 'machine_type'):
        identity = str(field(name) or '').casefold()
        if re.search(r'\bkobra[\s_-]*(?:s1|3)\b', identity):
            return 'printer_dispatch_unsupported: ' + KOBRA_DISPATCH_DETAIL
    return None
