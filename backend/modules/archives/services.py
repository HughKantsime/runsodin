"""Public archive services for printer-monitor callers.

Implementations remain within this module. Callers retain transaction ownership.
"""
from .attempt_accounting import capture_attempt_spools

__all__ = ["capture_attempt_spools"]
