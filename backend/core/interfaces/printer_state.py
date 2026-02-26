# core/interfaces/printer_state.py
from abc import ABC, abstractmethod
from typing import Optional


class PrinterStateProvider(ABC):
    """What other modules need from the printers module."""

    @abstractmethod
    def get_printer_status(self, printer_id: int) -> dict:
        """Returns: {state, progress, temps, ams_state, online}"""
        ...

    @abstractmethod
    def get_printer_info(self, printer_id: int) -> dict:
        """Returns: {id, name, model, api_type, ip, org_id, bed_x, bed_y}"""
        ...

    @abstractmethod
    def get_available_printers(self, org_id: Optional[int] = None) -> list:
        """Returns list of printers that are online and idle."""
        ...

    @abstractmethod
    def get_printer_slots(self, printer_id: int) -> list:
        """Returns filament slot data for color-match scoring."""
        ...
