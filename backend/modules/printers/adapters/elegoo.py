"""
Elegoo SDCP Adapter — WebSocket client for Elegoo 3D printers.

Supports: Centauri Carbon (FDM), Neptune 4 series, Saturn series (resin)
Protocol: SDCP v3.0.0 (Smart Device Control Protocol)
Transport: WebSocket (ws://printer_ip:3030/websocket) + UDP discovery (port 3000)
Auth: None required

Reference:
  - SDCP spec: https://docs.opencentauri.cc/software/api/
  - GitHub: https://github.com/WalkerFrederick/sdcp-centauri-carbon
  - GitHub: https://github.com/RemmyLee/carbon

Architecture note:
  This is the adapter (protocol client). The monitor (daemon) is elegoo_monitor.py.
  Same split as moonraker_adapter.py / moonraker_monitor.py.

SDCP Status Response (from WebSocket):
{
    "Status": {
        "CurrentStatus": [0],
        "TempOfHotbed": 67.5,
        "TempOfNozzle": 115.3,
        "TempOfBox": 26.4,
        "TempTargetHotbed": 0,
        "TempTargetNozzle": 0,
        "TempTargetBox": 0,
        "CurrentFanSpeed": {"ModelFan": 0, "AuxiliaryFan": 0, "BoxFan": 0},
        "PrintInfo": {
            "Status": 8,
            "CurrentLayer": 42,
            "TotalLayer": 165,
            "CurrentTicks": 3600,
            "TotalTicks": 9749,
            "Filename": "model.gcode",
            "Progress": 25
        }
    },
    "MainboardID": "...",
    "TimeStamp": 1752339395,
    "Topic": "sdcp/status/{MainboardID}"
}
"""

import json
import uuid
import time
import socket
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable
from enum import IntEnum

log = logging.getLogger(__name__)


class SDCPCurrentStatus(IntEnum):
    """SDCP CurrentStatus values."""
    IDLE = 0
    PRINTING = 1
    EXPOSURE_TESTING = 2  # resin
    DEVICE_TESTING = 3
    PAUSED = 4
    FILE_TRANSFER = 5
    LEVELING = 6
    CALIBRATING = 7
    HEATING = 8           # FDM pre-heat
    HOMING = 9
    BUSY = 10
    FEEDING = 11          # filament loading


class SDCPPrintStatus(IntEnum):
    """SDCP PrintInfo.Status values."""
    IDLE = 0
    HOMING = 1
    DROPPING = 2        # resin
    EXPOSING = 3        # resin
    LIFTING = 4         # resin
    PAUSED = 5
    PAUSING = 6
    STOPPING = 7
    PRINTING = 8        # FDM active printing
    COMPLETE = 16
    FILE_CHECKING = 17


class SDCPCommand(IntEnum):
    """SDCP command codes (Centauri Carbon / Neptune FDM)."""
    STATUS_REQUEST = 0
    START_PRINT = 128
    PAUSE_PRINT = 129
    STOP_PRINT = 130
    RESUME_PRINT = 131
    SET_NAME = 192
    SET_PRINT_SPEED = 403


@dataclass
class ElegooStatus:
    """Parsed status from an Elegoo SDCP printer."""
    connected: bool = False
    internal_state: str = "OFFLINE"

    # Temperatures (FDM)
    bed_temp: float = 0.0
    bed_target: float = 0.0
    nozzle_temp: float = 0.0
    nozzle_target: float = 0.0
    box_temp: float = 0.0
    box_target: float = 0.0

    # Print progress
    filename: str = ""
    progress_percent: float = 0.0
    current_layer: int = 0
    total_layers: int = 0
    current_ticks: int = 0  # elapsed seconds
    total_ticks: int = 0    # estimated total seconds
    time_remaining: int = 0 # seconds remaining

    # Status codes
    current_status: int = 0
    print_status: int = 0

    # Fan speeds
    model_fan: int = 0
    auxiliary_fan: int = 0
    box_fan: int = 0

    # Printer identity
    mainboard_id: str = ""
    printer_name: str = ""
    machine_name: str = ""
    firmware_version: str = ""

    # Raw data
    raw_data: Dict[str, Any] = field(default_factory=dict)


class ElegooPrinter:
    """
    Client for Elegoo SDCP protocol over WebSocket.

    Connects to ws://printer_ip:3030/websocket for real-time bidirectional
    communication. Also supports UDP discovery on port 3000.
    """

    def __init__(self, host: str, port: int = 3030, mainboard_id: str = ""):
        self.host = host
        self.port = port
        self.mainboard_id = mainboard_id
        self.ws_url = f"ws://{host}:{port}/websocket"
        self._ws = None
        self._connected = False
        self._latest_status = ElegooStatus()
        self._status_lock = threading.Lock()
        self._on_status_callback: Optional[Callable] = None

    @staticmethod
    def discover(timeout: float = 3.0, broadcast_addr: str = "255.255.255.255") -> List[Dict]:
        """
        UDP broadcast discovery on port 3000.

        Send "M99999" to broadcast, printers respond with JSON status blob.
        Returns list of discovered printer info dicts.
        """
        discovered = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)

        try:
            sock.sendto(b"M99999", (broadcast_addr, 3000))
            deadline = time.time() + timeout

            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                    try:
                        info = json.loads(data.decode("utf-8"))
                        # Normalize — some printers nest under "Data"
                        if "Data" in info:
                            attrs = info["Data"].get("Attributes", info["Data"])
                        else:
                            attrs = info

                        discovered.append({
                            "ip": addr[0],
                            "name": attrs.get("Name", "Unknown"),
                            "machine_name": attrs.get("MachineName", ""),
                            "brand": attrs.get("BrandName", "ELEGOO"),
                            "mainboard_id": attrs.get("MainboardID", ""),
                            "firmware": attrs.get("FirmwareVersion", ""),
                            "protocol": attrs.get("ProtocolVersion", ""),
                        })
                    except (json.JSONDecodeError, KeyError):
                        pass
                except socket.timeout:
                    break

        except Exception as e:
            log.warning(f"SDCP discovery error: {e}")
        finally:
            sock.close()

        return discovered

    def connect(self) -> bool:
        """
        Connect to printer WebSocket.
        Uses websocket-client library (pip install websocket-client).
        """
        try:
            import websocket
        except ImportError:
            log.error("websocket-client not installed. Run: pip install websocket-client")
            return False

        try:
            self._ws = websocket.WebSocketApp(
                self.ws_url,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open,
            )
            # Run in background thread
            self._ws_thread = threading.Thread(
                target=self._ws.run_forever,
                kwargs={"ping_interval": 30, "ping_timeout": 10},
                daemon=True,
            )
            self._ws_thread.start()

            # Wait for connection (up to 5 seconds)
            for _ in range(50):
                if self._connected:
                    return True
                time.sleep(0.1)

            log.warning(f"SDCP connection timeout to {self.host}")
            return False

        except Exception as e:
            log.warning(f"SDCP connect failed to {self.host}: {e}")
            return False

    def disconnect(self):
        """Close WebSocket connection."""
        self._connected = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _on_open(self, ws):
        log.info(f"SDCP connected to {self.host}")
        self._connected = True

    def _on_close(self, ws, close_status_code, close_msg):
        log.info(f"SDCP disconnected from {self.host}")
        self._connected = False

    def _on_error(self, ws, error):
        log.warning(f"SDCP error from {self.host}: {error}")

    def _on_message(self, ws, message):
        """Parse incoming SDCP status messages."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        topic = data.get("Topic", "")

        # Status update
        if "sdcp/status/" in topic:
            self._parse_status(data)
        elif "sdcp/notice/" in topic:
            # Notification (error, completion, etc.)
            log.debug(f"SDCP notice from {self.host}: {data}")

    def _parse_status(self, data: Dict):
        """Parse sdcp/status message into ElegooStatus."""
        with self._status_lock:
            status = self._latest_status
            status.connected = True
            status.raw_data = data

            status.mainboard_id = data.get("MainboardID", status.mainboard_id)

            s = data.get("Status", {})
            if not s:
                # Some firmware nests under Data.Status
                s = data.get("Data", {}).get("Status", {})
            if not s:
                return

            # Current status code
            cs = s.get("CurrentStatus", [0])
            status.current_status = cs[0] if isinstance(cs, list) else cs

            # Temperatures
            status.bed_temp = s.get("TempOfHotbed", 0.0)
            status.nozzle_temp = s.get("TempOfNozzle", 0.0)
            status.box_temp = s.get("TempOfBox", 0.0)
            status.bed_target = s.get("TempTargetHotbed", 0.0)
            status.nozzle_target = s.get("TempTargetNozzle", 0.0)
            status.box_target = s.get("TempTargetBox", 0.0)

            # Fan speeds
            fans = s.get("CurrentFanSpeed", {})
            status.model_fan = fans.get("ModelFan", 0)
            status.auxiliary_fan = fans.get("AuxiliaryFan", 0)
            status.box_fan = fans.get("BoxFan", 0)

            # Print info
            pi = s.get("PrintInfo", {})
            if pi:
                status.print_status = pi.get("Status", 0)
                status.current_layer = pi.get("CurrentLayer", 0)
                status.total_layers = pi.get("TotalLayer", 0)
                status.current_ticks = pi.get("CurrentTicks", 0)
                status.total_ticks = pi.get("TotalTicks", 0)
                status.filename = pi.get("Filename", "")
                status.progress_percent = pi.get("Progress", 0.0)

                # Calculate remaining time
                if status.total_ticks > 0 and status.current_ticks > 0:
                    status.time_remaining = max(0, status.total_ticks - status.current_ticks)
                else:
                    status.time_remaining = 0

            # Map to O.D.I.N. internal state
            if status.print_status == SDCPPrintStatus.PRINTING or status.current_status == SDCPCurrentStatus.PRINTING:
                status.internal_state = "PRINTING"
            elif status.print_status in (SDCPPrintStatus.PAUSED, SDCPPrintStatus.PAUSING):
                status.internal_state = "PAUSED"
            elif status.print_status == SDCPPrintStatus.COMPLETE:
                status.internal_state = "FINISHED"
            elif status.print_status == SDCPPrintStatus.STOPPING:
                status.internal_state = "STOPPING"
            elif status.current_status == SDCPCurrentStatus.HEATING:
                status.internal_state = "HEATING"
            elif status.current_status == SDCPCurrentStatus.HOMING:
                status.internal_state = "HOMING"
            elif status.current_status == SDCPCurrentStatus.LEVELING:
                status.internal_state = "LEVELING"
            else:
                status.internal_state = "IDLE"

        # Invoke callback if registered
        if self._on_status_callback:
            self._on_status_callback(self._latest_status)

    def get_status(self) -> ElegooStatus:
        """Return latest parsed status."""
        with self._status_lock:
            return self._latest_status

    def on_status(self, callback: Callable):
        """Register callback for status updates."""
        self._on_status_callback = callback

    def _send_command(self, cmd: int, cmd_data: Optional[Dict] = None) -> bool:
        """Send SDCP command to printer."""
        if not self._connected or not self._ws:
            return False

        payload = {
            "Id": str(uuid.uuid4()),
            "Data": {
                "Cmd": cmd,
                "Data": cmd_data or {},
                "RequestID": str(uuid.uuid4()),
                "MainboardID": self.mainboard_id,
                "TimeStamp": int(time.time()),
                "From": 0,
            },
            "Topic": f"sdcp/request/{self.mainboard_id}",
        }

        try:
            self._ws.send(json.dumps(payload))
            return True
        except Exception as e:
            log.warning(f"SDCP command {cmd} failed: {e}")
            return False

    def get_webcam_url(self) -> Optional[str]:
        """Discover camera stream URL for Elegoo FDM printers.

        Neptune 4 / Centauri Carbon expose an MJPEG stream on port 8080.
        Resin printers (Saturn) typically have no camera.
        """
        import requests as _req
        # Common camera endpoints for Elegoo FDM printers
        candidates = [
            f"http://{self.host}:8080/?action=stream",
            f"http://{self.host}:8080/webcam/?action=stream",
        ]
        for url in candidates:
            try:
                resp = _req.head(url, timeout=3)
                if resp.status_code == 200:
                    return url
            except Exception:
                continue
        return None

    def pause_print(self) -> bool:
        """Pause current print (Cmd 129)."""
        return self._send_command(SDCPCommand.PAUSE_PRINT)

    def resume_print(self) -> bool:
        """Resume paused print (Cmd 131)."""
        return self._send_command(SDCPCommand.RESUME_PRINT)

    def stop_print(self) -> bool:
        """Stop current print (Cmd 130)."""
        return self._send_command(SDCPCommand.STOP_PRINT)

    def set_print_speed(self, speed_pct: int) -> bool:
        """Set print speed percentage (Cmd 403)."""
        return self._send_command(SDCPCommand.SET_PRINT_SPEED, {"PrintSpeedPct": speed_pct})
