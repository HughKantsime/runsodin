"""
Printer Adapters

Unified module that imports and registers all printer adapters.
Import this module to get access to all supported printer types.

Supported printers:
- Bambu Lab (X1C, X1, P1P, P1S, A1, A1 Mini) via local MQTT
- OctoPrint (any printer) via REST API [placeholder]
- Moonraker/Klipper via REST API [placeholder]

Usage:
    from adapters import get_adapter, test_printer_connection
    
    # Get adapter for a printer
    adapter = get_adapter(
        api_type="bambu",
        name="My X1C",
        host="192.168.1.100",
        api_key="12345678",
        serial="00M00A000000000"  # Bambu-specific
    )
    
    # Connect and get status
    if adapter.connect():
        status = adapter.get_status()
        print(f"State: {status.state}")
"""

from typing import Optional, Dict, Any
from printer_adapter import (
    PrinterAdapter, 
    PrinterStatus, 
    PrinterState,
    FilamentInfo,
    register_adapter, 
    get_adapter,
    list_adapter_types
)


# ============== Bambu Lab Adapter ==============

class BambuAdapter(PrinterAdapter):
    """Bambu Lab printer adapter (X1C, X1, P1P, P1S, A1, A1 Mini)."""
    
    def __init__(
        self,
        name: str,
        host: str,
        api_key: Optional[str] = None,  # Access code
        serial: Optional[str] = None,   # Serial number
        **kwargs
    ):
        super().__init__(name, host, api_key, **kwargs)
        self.serial = serial
        self._bambu = None
    
    def connect(self) -> bool:
        from bambu_adapter import BambuPrinter
        
        if not self.serial or not self.api_key:
            return False
        
        self._bambu = BambuPrinter(
            ip=self.host,
            serial=self.serial,
            access_code=self.api_key
        )
        self._connected = self._bambu.connect()
        return self._connected
    
    def disconnect(self):
        if self._bambu:
            self._bambu.disconnect()
        self._connected = False
    
    def get_status(self) -> PrinterStatus:
        if not self._bambu:
            return PrinterStatus(state=PrinterState.OFFLINE)
        
        bambu_status = self._bambu.get_status()
        
        # Convert Bambu status to universal format
        status = PrinterStatus(
            state=PrinterState(bambu_status.state.value),
            progress_percent=bambu_status.print_progress,
            layer_current=bambu_status.layer_current,
            layer_total=bambu_status.layer_total,
            time_remaining_minutes=bambu_status.time_remaining_minutes,
            current_file=bambu_status.current_file,
            bed_temp=bambu_status.bed_temp,
            bed_target=bambu_status.bed_target,
            nozzle_temp=bambu_status.nozzle_temp,
            nozzle_target=bambu_status.nozzle_target,
            raw_data=bambu_status.raw_data
        )
        
        # Convert AMS slots
        for slot in bambu_status.ams_slots:
            status.filament_slots.append(FilamentInfo(
                slot_number=slot.slot_number,
                filament_type=slot.filament_type,
                color=slot.color,
                color_hex=slot.color_hex,
                remaining_percent=slot.remaining_percent
            ))
        
        return status
    
    def start_print(self, file_path: str) -> bool:
        # Bambu requires uploading via FTP then triggering
        # This is more complex - placeholder for now
        raise NotImplementedError("Use Bambu Studio or Handy to start prints for now")
    
    def pause_print(self) -> bool:
        return self._bambu.pause_print() if self._bambu else False
    
    def resume_print(self) -> bool:
        return self._bambu.resume_print() if self._bambu else False
    
    def cancel_print(self) -> bool:
        return self._bambu.stop_print() if self._bambu else False
    
    def send_gcode(self, gcode: str) -> bool:
        return self._bambu.send_gcode(gcode) if self._bambu else False
    
    def set_temperatures(self, bed: Optional[int] = None, nozzle: Optional[int] = None) -> bool:
        if not self._bambu:
            return False
        success = True
        if bed is not None:
            success = success and self._bambu.set_bed_temp(bed)
        if nozzle is not None:
            success = success and self._bambu.set_nozzle_temp(nozzle)
        return success


# Register Bambu adapter
register_adapter("bambu", BambuAdapter)


# ============== OctoPrint Adapter (Placeholder) ==============

class OctoPrintAdapter(PrinterAdapter):
    """
    OctoPrint adapter via REST API.
    
    Works with any printer running OctoPrint.
    """
    
    def connect(self) -> bool:
        # TODO: Implement OctoPrint REST connection
        # GET http://{host}/api/connection
        # Headers: X-Api-Key: {api_key}
        raise NotImplementedError("OctoPrint adapter coming soon")
    
    def disconnect(self):
        pass
    
    def get_status(self) -> PrinterStatus:
        # TODO: GET /api/job, /api/printer
        raise NotImplementedError("OctoPrint adapter coming soon")
    
    def start_print(self, file_path: str) -> bool:
        # TODO: POST /api/files/{location}/{filename}
        raise NotImplementedError("OctoPrint adapter coming soon")
    
    def pause_print(self) -> bool:
        # TODO: POST /api/job {"command": "pause"}
        raise NotImplementedError("OctoPrint adapter coming soon")
    
    def resume_print(self) -> bool:
        # TODO: POST /api/job {"command": "resume"}
        raise NotImplementedError("OctoPrint adapter coming soon")
    
    def cancel_print(self) -> bool:
        # TODO: POST /api/job {"command": "cancel"}
        raise NotImplementedError("OctoPrint adapter coming soon")


# Register OctoPrint adapter
register_adapter("octoprint", OctoPrintAdapter)


# ============== Moonraker/Klipper Adapter (Placeholder) ==============

class MoonrakerAdapter(PrinterAdapter):
    """
    Moonraker adapter for Klipper-based printers.
    
    REST API + optional WebSocket for real-time updates.
    """
    
    def connect(self) -> bool:
        # TODO: GET http://{host}/server/info
        raise NotImplementedError("Moonraker adapter coming soon")
    
    def disconnect(self):
        pass
    
    def get_status(self) -> PrinterStatus:
        # TODO: GET /printer/objects/query
        raise NotImplementedError("Moonraker adapter coming soon")
    
    def start_print(self, file_path: str) -> bool:
        # TODO: POST /printer/print/start?filename={file}
        raise NotImplementedError("Moonraker adapter coming soon")
    
    def pause_print(self) -> bool:
        # TODO: POST /printer/print/pause
        raise NotImplementedError("Moonraker adapter coming soon")
    
    def resume_print(self) -> bool:
        # TODO: POST /printer/print/resume
        raise NotImplementedError("Moonraker adapter coming soon")
    
    def cancel_print(self) -> bool:
        # TODO: POST /printer/print/cancel
        raise NotImplementedError("Moonraker adapter coming soon")


# Register Moonraker adapter
register_adapter("moonraker", MoonrakerAdapter)
register_adapter("klipper", MoonrakerAdapter)  # Alias


# ============== Helper Functions ==============

def test_printer_connection(
    api_type: str,
    host: str,
    api_key: Optional[str] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Test connection to a printer.
    
    Args:
        api_type: Printer type ("bambu", "octoprint", "moonraker")
        host: Printer IP/hostname
        api_key: Authentication key
        **kwargs: Adapter-specific options
        
    Returns:
        Dict with success status and printer info
    """
    result = {
        "success": False,
        "api_type": api_type,
        "host": host,
        "state": "offline",
        "error": None
    }
    
    try:
        adapter = get_adapter(
            api_type=api_type,
            name="test",
            host=host,
            api_key=api_key,
            **kwargs
        )
        
        if not adapter:
            result["error"] = f"Unknown printer type: {api_type}"
            return result
        
        if adapter.connect():
            status = adapter.get_status()
            result["success"] = True
            result["state"] = status.state.value
            result["bed_temp"] = status.bed_temp
            result["nozzle_temp"] = status.nozzle_temp
            result["filament_slots"] = len(status.filament_slots)
            adapter.disconnect()
        else:
            result["error"] = "Connection failed"
            
    except NotImplementedError as e:
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)
    
    return result


# ============== Export ==============

__all__ = [
    "PrinterAdapter",
    "PrinterStatus",
    "PrinterState", 
    "FilamentInfo",
    "BambuAdapter",
    "OctoPrintAdapter",
    "MoonrakerAdapter",
    "get_adapter",
    "register_adapter",
    "list_adapter_types",
    "test_printer_connection",
]
