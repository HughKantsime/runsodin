"""Public archive services for printer-monitor callers.

Implementations remain within this module. Callers retain transaction ownership.
"""
from .attempt_accounting import capture_attempt_spools
from .access import assert_archive_access

__all__ = ["capture_attempt_spools", "assert_archive_access"]


def create_print_archive(*args, **kwargs):
    from .archive import create_print_archive as create
    return create(*args, **kwargs)
