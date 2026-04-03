"""
O.D.I.N. — Printer utility helpers.

Provides utilities used across the printers module and other modules
that interact with printer records.

Extracted from deps.py as part of the modular architecture refactor.
"""

from typing import Optional


def get_printer_api_key(printer) -> Optional[str]:
    """Get decrypted API key for a printer."""
    if not printer.api_key:
        return None
    from core.crypto import decrypt
    return decrypt(printer.api_key)
