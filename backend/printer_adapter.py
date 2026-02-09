"""
Printer Adapter Interface

Base class and common interface for all printer types.
Each printer type (Bambu, OctoPrint, Moonraker, etc.) implements this interface.

This allows the scheduler to work with any printer type without 
knowing the underlying protocol (MQTT, REST, websocket, etc.)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any


class PrinterState(str, Enum):
    """Universal printer state."""
    IDLE = "idle"
    PRINTING = "printing"
    PAUSED = "paused"
    ERROR = "error"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class FilamentInfo:
    """Filament slot information."""
    slot_number: int
    filament_type: str = ""  # PLA, PETG, ABS, etc.
    color: str = ""
    color_hex: str = ""
    remaining_percent: int = 100
    spool_id: Optional[int] = None  # Spoolman ID if linked


@dataclass
class PrinterStatus:
    """Universal printer status."""
    state: PrinterState = PrinterState.UNKNOWN
    
    # Print progress
    progress_percent: int = 0
    layer_current: int = 0
    layer_total: int = 0
    time_remaining_minutes: int = 0
    time_elapsed_minutes: int = 0
    current_file: str = ""
    
    # Temperatures
    bed_temp: float = 0.0
    bed_target: float = 0.0
    nozzle_temp: float = 0.0
    nozzle_target: float = 0.0
    chamber_temp: float = 0.0
    
    # Filament
    filament_slots: List[FilamentInfo] = field(default_factory=list)
    active_slot: int = 0
    
    # Errors
    error_code: Optional[str] = None
    error_message: str = ""
    
    # Raw data from printer (for debugging)
    raw_data: Dict[str, Any] = field(default_factory=dict)


class PrinterAdapter(ABC):
    """
    Abstract base class for printer adapters.
    
    Each printer type implements this interface:
    - BambuAdapter (MQTT)
    - OctoPrintAdapter (REST)
    - MoonrakerAdapter (REST/WebSocket)
    - PrusaConnectAdapter (REST)
    """
    
    def __init__(
        self,
        name: str,
        host: str,
        api_key: Optional[str] = None,
        **kwargs
    ):
        """
        Initialize printer adapter.
        
        Args:
            name: Friendly name for this printer
            host: IP address or hostname
            api_key: Authentication key/token/code
            **kwargs: Adapter-specific options
        """
        self.name = name
        self.host = host
        self.api_key = api_key
        self._connected = False
    
    @property
    def connected(self) -> bool:
        """Check if connected to printer."""
        return self._connected
    
    @abstractmethod
    def connect(self) -> bool:
        """
        Connect to the printer.
        
        Returns:
            True if connection successful
        """
        pass
    
    @abstractmethod
    def disconnect(self):
        """Disconnect from printer."""
        pass
    
    @abstractmethod
    def get_status(self) -> PrinterStatus:
        """
        Get current printer status.
        
        Returns:
            PrinterStatus with current state
        """
        pass
    
    @abstractmethod
    def start_print(self, file_path: str) -> bool:
        """
        Start a print job.
        
        Args:
            file_path: Path to file on printer or URL
            
        Returns:
            True if print started successfully
        """
        pass
    
    @abstractmethod
    def pause_print(self) -> bool:
        """Pause current print."""
        pass
    
    @abstractmethod
    def resume_print(self) -> bool:
        """Resume paused print."""
        pass
    
    @abstractmethod
    def cancel_print(self) -> bool:
        """Cancel/stop current print."""
        pass
    
    # ============== Optional Methods (override if supported) ==============
    
    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """
        Upload a file to the printer.
        
        Args:
            local_path: Local file path
            remote_path: Destination path on printer
            
        Returns:
            True if upload successful
        """
        raise NotImplementedError("File upload not supported for this printer type")
    
    def get_files(self) -> List[str]:
        """
        List files on printer.
        
        Returns:
            List of file paths
        """
        return []
    
    def delete_file(self, file_path: str) -> bool:
        """Delete a file from printer."""
        raise NotImplementedError("File deletion not supported for this printer type")
    
    def set_temperatures(
        self,
        bed: Optional[int] = None,
        nozzle: Optional[int] = None
    ) -> bool:
        """Set target temperatures."""
        raise NotImplementedError("Temperature control not supported")
    
    def send_gcode(self, gcode: str) -> bool:
        """Send raw G-code command."""
        raise NotImplementedError("G-code sending not supported")
    
    def home_axes(self, axes: str = "XYZ") -> bool:
        """Home specified axes."""
        return self.send_gcode(f"G28 {axes}")


# ============== Adapter Registry ==============

_adapters: Dict[str, type] = {}


def register_adapter(api_type: str, adapter_class: type):
    """Register a printer adapter class."""
    _adapters[api_type.lower()] = adapter_class


def get_adapter(
    api_type: str,
    name: str,
    host: str,
    api_key: Optional[str] = None,
    **kwargs
) -> Optional[PrinterAdapter]:
    """
    Get a printer adapter instance.
    
    Args:
        api_type: Printer type ("bambu", "octoprint", "moonraker")
        name: Printer name
        host: Printer IP/hostname
        api_key: Authentication key
        **kwargs: Adapter-specific options
        
    Returns:
        PrinterAdapter instance or None if type not found
    """
    adapter_class = _adapters.get(api_type.lower())
    if not adapter_class:
        return None
    return adapter_class(name=name, host=host, api_key=api_key, **kwargs)


def list_adapter_types() -> List[str]:
    """List registered adapter types."""
    return list(_adapters.keys())
