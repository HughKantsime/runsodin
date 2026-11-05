"""Public archive services for printer-monitor callers.

Implementations remain within this module. Callers retain transaction ownership.
"""
from .attempt_accounting import capture_attempt_spools
from .access import assert_archive_access

__all__ = ["capture_attempt_spools", "assert_archive_access"]
