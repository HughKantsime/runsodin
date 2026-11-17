"""Service-layer entry points other modules may import (the sanctioned cross-module boundary)."""


def observe_printer_detection(monitor, raw, status):
    """The printer's own camera reports (docs/bambu-hms.md): see printer_native.observe. Never raises."""
    from modules.vision.printer_native import observe
    return observe(monitor, raw, status)
