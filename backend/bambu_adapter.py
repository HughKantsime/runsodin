"""
Bambu Lab Printer Adapter

Connects to Bambu printers via local MQTT for:
- Status monitoring (idle, printing, error)
- Print progress (%, time remaining)
- AMS filament state
- Starting/stopping prints

Works with X1C, X1, P1P, P1S, A1, A1 Mini (any printer with LAN access)

Usage:
    from bambu_adapter import BambuPrinter
    
    printer = BambuPrinter(
        ip="192.168.1.100",
        serial="00M00A000000000",  # Your printer serial
        access_code="12345678"      # From printer screen
    )
    
    printer.connect()
    status = printer.get_status()
    print(status)
    printer.disconnect()
"""

import json
import ssl
import time
import threading
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
import paho.mqtt.client as mqtt


class PrinterState(str, Enum):
    """Printer state enumeration."""
    IDLE = "idle"
    PRINTING = "printing"
    PAUSED = "paused"
    ERROR = "error"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class AMSSlot:
    """Represents a single AMS filament slot."""
    slot_number: int
    filament_type: str = ""
    color: str = ""
    color_hex: str = ""
    remaining_percent: int = 0
    empty: bool = True


@dataclass 
class PrinterStatus:
    """Current printer status."""
    state: PrinterState = PrinterState.UNKNOWN
    print_progress: int = 0  # 0-100
    layer_current: int = 0
    layer_total: int = 0
    time_remaining_minutes: int = 0
    current_file: str = ""
    bed_temp: float = 0.0
    bed_target: float = 0.0
    nozzle_temp: float = 0.0
    nozzle_target: float = 0.0
    fan_speed: int = 0
    ams_slots: list = field(default_factory=list)
    error_message: str = ""
    raw_data: Dict = field(default_factory=dict)


class BambuPrinter:
    """
    Bambu Lab printer interface via local MQTT.
    
    The printer runs an MQTT broker on port 8883 (TLS).
    Authentication uses the printer's serial number and access code.
    """
    
    MQTT_PORT = 8883
    MQTT_TIMEOUT = 10
    
    def __init__(
        self,
        ip: str,
        serial: str,
        access_code: str,
        on_status_update: Optional[Callable[[PrinterStatus], None]] = None
    ):
        """
        Initialize Bambu printer connection.
        
        Args:
            ip: Printer IP address (e.g., "192.168.1.100")
            serial: Printer serial number (e.g., "00M00A000000000")
            access_code: Access code from printer screen (e.g., "12345678")
            on_status_update: Optional callback for real-time status updates
        """
        self.ip = ip
        self.serial = serial
        self.access_code = access_code
        self.on_status_update = on_status_update
        
        self._client: Optional[mqtt.Client] = None
        self._connected = False
        self._status = PrinterStatus()
        self._lock = threading.Lock()
        
        # MQTT topics for this printer
        self._topic_report = f"device/{serial}/report"
        self._topic_request = f"device/{serial}/request"
    
    def connect(self) -> bool:
        """
        Connect to printer MQTT broker.
        
        Returns:
            True if connected successfully
        """
        try:
            # Create MQTT client
            self._client = mqtt.Client(
                client_id=f"printfarm_{self.serial}",
                protocol=mqtt.MQTTv311
            )
            
            # Set credentials (username is "bblp", password is access code)
            self._client.username_pw_set("bblp", self.access_code)
            
            # Configure TLS (Bambu uses self-signed certs)
            self._client.tls_set(cert_reqs=ssl.CERT_NONE)
            self._client.tls_insecure_set(True)
            
            # Set callbacks
            self._client.on_connect = self._on_connect
            self._client.on_message = self._on_message
            self._client.on_disconnect = self._on_disconnect
            
            # Connect
            self._client.connect(self.ip, self.MQTT_PORT, keepalive=60)
            self._client.loop_start()
            
            # Wait for connection
            timeout = time.time() + self.MQTT_TIMEOUT
            while not self._connected and time.time() < timeout:
                time.sleep(0.1)
            
            return self._connected
            
        except Exception as e:
            print(f"Connection error: {e}")
            self._status.state = PrinterState.OFFLINE
            return False
    
    def disconnect(self):
        """Disconnect from printer."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
    
    def get_status(self) -> PrinterStatus:
        """Get current printer status."""
        with self._lock:
            return self._status
    
    def request_full_status(self):
        """Request a full status update from the printer."""
        if not self._connected:
            return
        
        # Send pushall request to get complete state
        payload = {
            "pushing": {
                "sequence_id": "0",
                "command": "pushall"
            }
        }
        self._publish(payload)
    
    def pause_print(self) -> bool:
        """Pause current print."""
        return self._send_gcode("M400\nM25")
    
    def resume_print(self) -> bool:
        """Resume paused print."""
        return self._send_gcode("M400\nM24")
    
    def stop_print(self) -> bool:
        """Stop/cancel current print."""
        if not self._connected:
            return False
        
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "stop"
            }
        }
        return self._publish(payload)
    
    def send_gcode(self, gcode: str) -> bool:
        """
        Send raw G-code to printer.
        
        Args:
            gcode: G-code command(s), can be multiline
        """
        return self._send_gcode(gcode)
    
    def set_bed_temp(self, temp: int) -> bool:
        """Set bed temperature."""
        return self._send_gcode(f"M140 S{temp}")
    
    def set_nozzle_temp(self, temp: int) -> bool:
        """Set nozzle temperature."""
        return self._send_gcode(f"M104 S{temp}")
    
    def turn_light_on(self) -> bool:
        """Turn chamber light on."""
        return self._send_gcode("M355 S1")
    
    def turn_light_off(self) -> bool:
        """Turn chamber light off."""
        return self._send_gcode("M355 S0")
    
    # ============== Private Methods ==============
    
    def _on_connect(self, client, userdata, flags, rc):
        """MQTT connect callback."""
        if rc == 0:
            self._connected = True
            # Subscribe to printer reports
            client.subscribe(self._topic_report)
            # Request initial status
            self.request_full_status()
        else:
            print(f"Connection failed with code: {rc}")
            self._status.state = PrinterState.OFFLINE
    
    def _on_disconnect(self, client, userdata, rc):
        """MQTT disconnect callback."""
        self._connected = False
        with self._lock:
            self._status.state = PrinterState.OFFLINE
    
    def _on_message(self, client, userdata, msg):
        """MQTT message callback - parse printer status updates."""
        try:
            payload = json.loads(msg.payload.decode())
            self._parse_status(payload)
        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"Error parsing message: {e}")
    
    def _parse_status(self, payload: Dict):
        """Parse printer status from MQTT payload."""
        with self._lock:
            self._status.raw_data = payload
            
            # Get the print section if present
            print_data = payload.get("print", {})
            
            if not print_data:
                return
            
            # Parse printer state
            gcode_state = print_data.get("gcode_state", "").upper()
            if gcode_state == "IDLE":
                self._status.state = PrinterState.IDLE
            elif gcode_state in ("RUNNING", "PREPARE"):
                self._status.state = PrinterState.PRINTING
            elif gcode_state == "PAUSE":
                self._status.state = PrinterState.PAUSED
            elif gcode_state in ("FAILED", "FINISH"):
                self._status.state = PrinterState.IDLE
            else:
                self._status.state = PrinterState.UNKNOWN
            
            # Parse progress
            self._status.print_progress = print_data.get("mc_percent", 0)
            self._status.layer_current = print_data.get("layer_num", 0)
            self._status.layer_total = print_data.get("total_layer_num", 0)
            self._status.time_remaining_minutes = print_data.get("mc_remaining_time", 0)
            self._status.current_file = print_data.get("gcode_file", "")
            
            # Parse temperatures
            self._status.bed_temp = print_data.get("bed_temper", 0.0)
            self._status.bed_target = print_data.get("bed_target_temper", 0.0)
            self._status.nozzle_temp = print_data.get("nozzle_temper", 0.0)
            self._status.nozzle_target = print_data.get("nozzle_target_temper", 0.0)
            
            # Parse fan
            self._status.fan_speed = print_data.get("cooling_fan_speed", 0)
            
            # Parse AMS state
            ams_data = print_data.get("ams", {})
            if ams_data:
                self._parse_ams(ams_data)
            
            # Check for errors
            if print_data.get("print_error", 0) != 0:
                self._status.state = PrinterState.ERROR
                self._status.error_message = str(print_data.get("print_error"))
            
            # Callback if registered
            if self.on_status_update:
                self.on_status_update(self._status)
    
    def _parse_ams(self, ams_data: Dict):
        """Parse AMS filament slot data."""
        self._status.ams_slots = []
        
        ams_units = ams_data.get("ams", [])
        slot_num = 1
        
        for unit in ams_units:
            trays = unit.get("tray", [])
            for tray in trays:
                slot = AMSSlot(
                    slot_number=slot_num,
                    filament_type=tray.get("tray_type", ""),
                    color=tray.get("tray_color", ""),
                    color_hex=tray.get("tray_color", "")[:6] if tray.get("tray_color") else "",
                    remaining_percent=int(tray.get("remain", 0)),
                    empty=tray.get("tray_type", "") == ""
                )
                self._status.ams_slots.append(slot)
                slot_num += 1
    
    def _send_gcode(self, gcode: str) -> bool:
        """Send G-code command to printer."""
        if not self._connected:
            return False
        
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "gcode_line",
                "param": gcode
            }
        }
        return self._publish(payload)
    
    def _publish(self, payload: Dict) -> bool:
        """Publish message to printer."""
        if not self._client or not self._connected:
            return False
        
        try:
            result = self._client.publish(
                self._topic_request,
                json.dumps(payload)
            )
            return result.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            print(f"Publish error: {e}")
            return False


# ============== Convenience Functions ==============

def test_connection(ip: str, serial: str, access_code: str) -> Dict[str, Any]:
    """
    Test connection to a Bambu printer.
    
    Returns dict with connection status and basic info.
    """
    printer = BambuPrinter(ip, serial, access_code)
    
    result = {
        "success": False,
        "ip": ip,
        "serial": serial,
        "state": "offline",
        "error": None
    }
    
    try:
        if printer.connect():
            time.sleep(2)  # Wait for status update
            status = printer.get_status()
            result["success"] = True
            result["state"] = status.state.value
            result["bed_temp"] = status.bed_temp
            result["nozzle_temp"] = status.nozzle_temp
            result["ams_slots"] = len(status.ams_slots)
        else:
            result["error"] = "Connection failed - check IP, serial, and access code"
    except Exception as e:
        result["error"] = str(e)
    finally:
        printer.disconnect()
    
    return result


# ============== CLI Test ==============

if __name__ == "__main__":
    # Test with your printer details
    PRINTER_IP = "192.168.1.100"      # Replace with your printer IP
    SERIAL = "00M00A000000000"        # Replace with your serial number
    ACCESS_CODE = "12345678"          # Replace with your access code
    
    print(f"Connecting to Bambu printer at {PRINTER_IP}...")
    
    result = test_connection(PRINTER_IP, SERIAL, ACCESS_CODE)
    
    if result["success"]:
        print(f"✓ Connected!")
        print(f"  State: {result['state']}")
        print(f"  Bed temp: {result['bed_temp']}°C")
        print(f"  Nozzle temp: {result['nozzle_temp']}°C")
        print(f"  AMS slots: {result['ams_slots']}")
    else:
        print(f"✗ Connection failed: {result['error']}")
