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
        ip="YOUR_PRINTER_IP",
        serial="YOUR_SERIAL_NUMBER",
        access_code="YOUR_ACCESS_CODE"
    )
    
    printer.connect()
    status = printer.get_status()
    print(status)
    printer.disconnect()
"""

import ftplib  # nosec B402
import json
import os
import socket
import ssl
import time
import threading
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
import paho.mqtt.client as mqtt


class _ImplicitFTPS(ftplib.FTP):
    """Implicit FTPS client for Bambu printers.

    Bambu uses implicit TLS on port 990: TLS is negotiated immediately upon
    connection, before any FTP commands. Python's ftplib.FTP_TLS only supports
    explicit FTPS (STARTTLS), so we wrap the socket in SSL ourselves.
    """

    def __init__(self):
        super().__init__()
        self._ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def connect(self, host='', port=990, timeout=30, **kwargs):
        self.host = host
        self.port = port
        self.timeout = timeout
        raw_sock = socket.create_connection((host, port), timeout=timeout)
        self.sock = self._ssl_ctx.wrap_socket(raw_sock, server_hostname=host)
        self.af = self.sock.family
        self.file = self.sock.makefile('r', encoding='latin-1')
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if not isinstance(conn, ssl.SSLSocket):
            conn = self._ssl_ctx.wrap_socket(conn, server_hostname=self.host)
        return conn, size


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
    rfid_tag: str = ""  # Bambu RFID tag_uid
    sub_brand: str = ""  # e.g., "PLA Galaxy", "PETG HF"
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
    printer_type: str = ""  # e.g. "X1C", "BL-P001" — raw value from MQTT printer_type field


class BambuPrinter:
    """
    Bambu Lab printer interface via local MQTT.
    
    The printer runs an MQTT broker on port 8883 (TLS).
    Authentication uses the printer's serial number and access code.
    """
    
    MQTT_PORT = 8883
    MQTT_TIMEOUT = 10
    FTPS_PORT = 990
    
    def __init__(
        self,
        ip: str,
        serial: str,
        access_code: str,
        on_status_update: Optional[Callable[[PrinterStatus], None]] = None,
        client_id: Optional[str] = None
    ):
        """
        Initialize Bambu printer connection.

        Args:
            ip: Printer IP address
            serial: Printer serial number (e.g., "00M00A000000000")
            access_code: Access code from printer screen (e.g., "12345678")
            on_status_update: Optional callback for real-time status updates
            client_id: MQTT client ID (default: printfarm_{serial}). Use a
                       unique value for command connections to avoid colliding
                       with the monitor daemon's persistent connection.
        """
        self.ip = ip
        self.serial = serial
        self.access_code = access_code
        self.on_status_update = on_status_update
        self._client_id = client_id or f"printfarm_{serial}"
        
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
                client_id=self._client_id,
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
        if not self._connected:
            return False
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "pause"
            }
        }
        return self._publish(payload)

    def resume_print(self) -> bool:
        """Resume paused print."""
        if not self._connected:
            return False
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "resume"
            }
        }
        return self._publish(payload)
    
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

    def set_fan_speed(self, fan: str, speed: int) -> bool:
        """Set fan speed via G-code M106.

        Args:
            fan: 'part_cooling' (P1), 'auxiliary' (P2), or 'chamber' (P3)
            speed: 0-255
        """
        fan_map = {"part_cooling": 1, "auxiliary": 2, "chamber": 3}
        p = fan_map.get(fan)
        if p is None:
            return False
        speed = max(0, min(255, speed))
        return self._send_gcode(f"M106 P{p} S{speed}")

    def refresh_ams_rfid(self) -> bool:
        """Trigger AMS RFID re-read for all trays."""
        if not self._connected:
            return False
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "ams_change_filament",
                "target": 255,  # 255 = trigger re-read without changing
                "curr_temp": 0,
                "tar_temp": 0
            }
        }
        return self._publish(payload)

    def set_ams_filament(self, ams_id: int, slot_id: int, material: str,
                         color: str, k_factor: float = 0.0) -> bool:
        """Configure an AMS slot's filament settings via MQTT.

        Args:
            ams_id: AMS unit index (0-based)
            slot_id: Tray index within the AMS (0-3)
            material: Filament type string (e.g., 'PLA', 'PETG')
            color: Hex color without # (e.g., 'FF5500FF')
            k_factor: Pressure advance K-factor
        """
        if not self._connected:
            return False
        # Pad color to 8 hex chars (RRGGBBAA) if needed
        color = color.lstrip("#")
        if len(color) == 6:
            color = color + "FF"
        tray_id = ams_id * 4 + slot_id
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "ams_filament_setting",
                "ams_id": ams_id,
                "tray_id": tray_id,
                "tray_info_idx": material,
                "tray_color": color.upper(),
                "nozzle_temp_min": 190,
                "nozzle_temp_max": 240,
                "tray_type": material,
                "setting_id": "",
                "k": k_factor
            }
        }
        return self._publish(payload)

    def clear_print_errors(self) -> bool:
        """Clear HMS/print errors on the printer."""
        if not self._connected:
            return False
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "clean_print_error"
            }
        }
        return self._publish(payload)

    def skip_objects(self, object_ids: list) -> bool:
        """Skip objects during an active print.

        Args:
            object_ids: List of object indices to skip (0-based).
        """
        if not self._connected:
            return False
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "skip_objects",
                "obj_list": [str(oid) for oid in object_ids]
            }
        }
        return self._publish(payload)

    def set_print_speed(self, speed_level: int) -> bool:
        """Set print speed profile.

        Args:
            speed_level: 1=Silent, 2=Standard, 3=Sport, 4=Ludicrous
        """
        if not self._connected:
            return False
        if speed_level not in (1, 2, 3, 4):
            return False
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "print_speed",
                "param": str(speed_level)
            }
        }
        return self._publish(payload)

    def upload_file(self, local_path: str, remote_filename: str = None) -> bool:
        """Upload a .3mf file to the printer via implicit FTPS (port 990).

        The printer must be reachable at self.ip. No MQTT connection is required.
        Upload uses passive mode. The file lands in the printer's root FTP directory.

        Args:
            local_path: Absolute path to the local .3mf file.
            remote_filename: Name to store on the printer (default: basename of local_path).

        Returns:
            True if the upload completed without error.
        """
        if remote_filename is None:
            remote_filename = os.path.basename(local_path)

        try:
            ftp = _ImplicitFTPS()
            ftp.connect(host=self.ip, port=self.FTPS_PORT, timeout=30)
            ftp.login(user="bblp", passwd=self.access_code)
            ftp.set_pasv(True)
            with open(local_path, 'rb') as f:
                ftp.storbinary(f"STOR {remote_filename}", f)
            ftp.quit()
            return True
        except Exception as e:
            print(f"[{self.serial}] FTPS upload error: {e}")
            return False

    def start_print(
        self,
        remote_filename: str,
        plate_num: int = 1,
        use_ams: bool = True,
        bed_leveling: bool = True,
        timelapse: bool = False,
    ) -> bool:
        """Send MQTT command to start printing an uploaded .3mf file.

        The file must already exist on the printer (uploaded via upload_file).
        Requires an active MQTT connection (call connect() first).

        Args:
            remote_filename: Filename on the printer (as passed to upload_file).
            plate_num: Plate number inside the .3mf to print (default 1).
            use_ams: Whether to use the AMS for filament feeding (default True).
            bed_leveling: Whether to run auto bed leveling before print (default True).
            timelapse: Whether to record a timelapse (default False).

        Returns:
            True if the MQTT command was published and acknowledged.
        """
        if not self._connected:
            return False

        subtask_name = remote_filename.replace('.3mf', '')
        payload = {
            "print": {
                "sequence_id": str(int(time.time()) % 100000),
                "command": "project_file",
                "param": f"Metadata/plate_{plate_num}.gcode",
                "subtask_name": subtask_name,
                "url": f"ftp:///{remote_filename}",
                "bed_type": "auto",
                "timelapse": timelapse,
                "bed_leveling": bed_leveling,
                "flow_cali": False,
                "vibration_cali": True,
                "layer_inspect": False,
                "use_ams": use_ams,
                "profile_id": "0",
                "project_id": "0",
                "subtask_id": "0",
                "task_id": "0",
            }
        }
        return self._publish(payload)

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
            
            # Capture printer_type (model code) from MQTT
            raw_pt = print_data.get("printer_type", "")
            if raw_pt:
                self._status.printer_type = raw_pt

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
                    empty=tray.get("tray_type", "") == "",
                    rfid_tag=tray.get("tag_uid", ""),
                    sub_brand=tray.get("tray_sub_brands", "")
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
        """Publish message to printer and wait for broker acknowledgement."""
        if not self._client or not self._connected:
            return False

        try:
            result = self._client.publish(
                self._topic_request,
                json.dumps(payload)
            )
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                return False
            # Block until the broker ACKs delivery (up to 5s)
            result.wait_for_publish(timeout=5)
            return result.is_published()
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
    PRINTER_IP = "YOUR_PRINTER_IP"
    SERIAL = "YOUR_SERIAL_NUMBER"
    ACCESS_CODE = "YOUR_ACCESS_CODE"
    
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
