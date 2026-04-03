"""
Printer adapters package.

Canonical adapter implementations:
- bambu.py     — BambuPrinter (MQTT)
- moonraker.py — MoonrakerPrinter (REST)
- prusalink.py — PrusaLinkPrinter (REST)
- elegoo.py    — ElegooPrinter (WebSocket/SDCP)
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class PrinterAdapter(ABC):
    """Base interface for all printer protocol adapters."""

    @abstractmethod
    def get_status(self) -> Any:
        """Return current printer status."""

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the printer."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection to the printer."""
