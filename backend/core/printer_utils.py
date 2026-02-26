"""
O.D.I.N. — Printer utility helpers.

Provides utilities used across the printers module and other modules
that interact with printer records.

Extracted from deps.py as part of the modular architecture refactor.
"""

from datetime import datetime, timezone
from typing import Optional


def compute_printer_online(printer_dict: dict) -> dict:
    """Add is_online field based on last_seen within 90 seconds."""
    if printer_dict.get("last_seen"):
        try:
            last = datetime.fromisoformat(str(printer_dict["last_seen"]))
            printer_dict["is_online"] = (datetime.now(timezone.utc) - last).total_seconds() < 90
        except Exception:
            printer_dict["is_online"] = False
    else:
        printer_dict["is_online"] = False
    return printer_dict


def get_printer_api_key(printer) -> Optional[str]:
    """Get decrypted API key for a printer."""
    if not printer.api_key:
        return None
    from core.crypto import decrypt
    return decrypt(printer.api_key)
