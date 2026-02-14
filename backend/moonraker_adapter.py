"""
O.D.I.N. — Moonraker Adapter

Connects to Klipper printers via Moonraker REST API for:
- Status monitoring (idle, printing, error, paused)
- Print progress (%, layers, time remaining)
- Temperature monitoring (bed, extruder, chamber)
- Fan speed, speed/flow factors
- MMU/ACE filament slot state
- Filament runout sensor detection
- Nozzle diameter from config
- Job control (start, pause, resume, cancel)
- Webcam URL discovery

Tested with:
- Anycubic Kobra S1 running Rinkhals (Moonraker on port 80)

Works with any Moonraker-compatible printer:
- Voron, Creality K1/K2, modded Enders, etc.

Usage:
    from moonraker_adapter import MoonrakerPrinter

    printer = MoonrakerPrinter(host="YOUR_PRINTER_IP", port=80)
    if printer.connect():
        status = printer.get_status()
        print(status)
        printer.disconnect()
"""

import json
import time
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

log = logging.getLogger("moonraker_adapter")


class MoonrakerState(str, Enum):
    """Printer state from Moonraker."""
    READY = "ready"
    PRINTING = "printing"
    PAUSED = "paused"
    ERROR = "error"
    STANDBY = "standby"
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    DISCONNECTED = "disconnected"


# Map Moonraker states to our internal states used by the dashboard/monitor
MOONRAKER_TO_INTERNAL_STATE = {
    MoonrakerState.READY: "IDLE",
    MoonrakerState.PRINTING: "RUNNING",
    MoonrakerState.PAUSED: "PAUSE",
    MoonrakerState.ERROR: "FAILED",
    MoonrakerState.STANDBY: "IDLE",
    MoonrakerState.STARTUP: "IDLE",
    MoonrakerState.SHUTDOWN: "IDLE",
    MoonrakerState.DISCONNECTED: "OFFLINE",
}


@dataclass
class MoonrakerFilamentSlot:
    """Single filament slot (MMU gate)."""
    gate: int
    material: str = ""
    color_hex: str = ""
    loaded: bool = False
    name: str = ""
    temperature: int = 0


@dataclass
class MoonrakerStatus:
    """Complete printer status snapshot."""
    state: MoonrakerState = MoonrakerState.DISCONNECTED
    internal_state: str = "OFFLINE"

    # Temperatures
    bed_temp: float = 0.0
    bed_target: float = 0.0
    nozzle_temp: float = 0.0
    nozzle_target: float = 0.0
    chamber_temp: Optional[float] = None

    # Fan & motion
    fan_speed: int = 0           # 0-100 (converted from Klipper's 0.0-1.0)
    speed_factor: float = 1.0   # gcode_move speed multiplier
    extrude_factor: float = 1.0 # gcode_move extrusion multiplier

    # Nozzle config (cached from configfile)
    nozzle_diameter: Optional[float] = None

    # Filament sensor
    filament_detected: Optional[bool] = None  # None = no sensor

    # Print progress
    filename: str = ""
    progress_percent: float = 0.0
    current_layer: int = 0
    total_layers: int = 0
    print_duration: float = 0.0
    filament_used_mm: float = 0.0

    # Error message (from webhooks.state_message on error)
    error_message: str = ""

    # Printer info
    device_type: str = ""
    klippy_state: str = ""

    # Filament slots (MMU/ACE)
    filament_slots: List[MoonrakerFilamentSlot] = field(default_factory=list)

    # Environment sensors (temp sensors associated with MMU/enclosure)
    environment_sensors: Dict[str, float] = field(default_factory=dict)

    # Webcam
    webcam_stream_url: str = ""
    webcam_snapshot_url: str = ""

    # Raw data for debugging
    raw_data: Dict[str, Any] = field(default_factory=dict)


class MoonrakerPrinter:
    """
    Client for Moonraker REST API.
    
    Handles all communication with a single Moonraker-based printer.
    No persistent connection needed — each call is a simple HTTP request.
    """
    
    # Sensor name patterns that indicate MMU/enclosure environment
    _ENV_SENSOR_KEYWORDS = ("ace", "mmu", "dryer", "drybox", "enclosure", "chamber", "filament_box")

    def __init__(self, host: str, port: int = 80, api_key: str = ""):
        self.host = host
        self.port = port
        self.api_key = api_key
        self.base_url = f"http://{host}:{port}" if port != 80 else f"http://{host}"
        self._connected = False
        self._device_type = ""
        self._webcam_stream = ""
        self._webcam_snapshot = ""
        self._nozzle_diameter: Optional[float] = None
        # Discovered optional objects (populated on connect)
        self._temperature_sensors: List[str] = []   # e.g. ["temperature_sensor chamber"]
        self._env_sensors: List[str] = []            # subset of above matching enclosure keywords
        self._filament_sensors: List[str] = []       # filament_switch_sensor / filament_motion_sensor
        self._has_fan: bool = False
    
    # ==================== Connection ====================
    
    def connect(self) -> bool:
        """Test connection to Moonraker and cache printer info."""
        try:
            info = self._get("/server/info")
            if not info or "result" not in info:
                log.error(f"Invalid response from {self.host}")
                return False
            
            result = info["result"]
            if not result.get("klippy_connected"):
                log.warning(f"{self.host}: Klippy not connected")
                return False
            
            # Cache device type
            printer_info = self._get("/printer/info")
            if printer_info and "result" in printer_info:
                self._device_type = printer_info["result"].get("device_type", "")

            # Discover available objects (sensors, fans, etc.)
            self._discover_objects()

            # Cache nozzle diameter from config (one-time)
            self._cache_nozzle_diameter()

            # Cache webcam URLs
            self._discover_webcam()

            self._connected = True
            log.info(f"Connected to {self._device_type or 'Moonraker'} at {self.host}")
            return True
            
        except Exception as e:
            log.error(f"Failed to connect to {self.host}: {e}")
            return False
    
    def disconnect(self):
        """No persistent connection to close, just mark disconnected."""
        self._connected = False
        log.info(f"Disconnected from {self.host}")
    
    @property
    def connected(self) -> bool:
        return self._connected
    
    # ==================== Object Discovery ====================

    def _discover_objects(self):
        """Query available Klipper objects and cache sensor/fan names."""
        try:
            data = self._get("/printer/objects/list")
            if not data or "result" not in data:
                return
            objects = data["result"].get("objects", [])

            self._has_fan = "fan" in objects
            self._temperature_sensors = [o for o in objects if o.startswith("temperature_sensor ")]
            self._filament_sensors = [
                o for o in objects
                if o.startswith("filament_switch_sensor ") or o.startswith("filament_motion_sensor ")
            ]

            # Identify environment sensors (MMU/enclosure/dryer)
            self._env_sensors = [
                s for s in self._temperature_sensors
                if any(kw in s.lower() for kw in self._ENV_SENSOR_KEYWORDS)
            ]

            discovered = []
            if self._has_fan:
                discovered.append("fan")
            if self._temperature_sensors:
                discovered.append(f"{len(self._temperature_sensors)} temp sensor(s)")
            if self._env_sensors:
                discovered.append(f"env: {', '.join(s.split(' ', 1)[1] for s in self._env_sensors)}")
            if self._filament_sensors:
                discovered.append(f"{len(self._filament_sensors)} filament sensor(s)")
            if discovered:
                log.info(f"Discovered objects on {self.host}: {', '.join(discovered)}")
        except Exception as e:
            log.warning(f"Object discovery failed on {self.host}: {e}")

    def _cache_nozzle_diameter(self):
        """Read nozzle diameter from Klipper config (one-time on connect)."""
        try:
            data = self._get("/printer/objects/query?configfile")
            if data and "result" in data:
                cfg = data["result"].get("status", {}).get("configfile", {})
                settings = cfg.get("settings", {})
                extruder_cfg = settings.get("extruder", {})
                nozzle = extruder_cfg.get("nozzle_diameter")
                if nozzle is not None:
                    self._nozzle_diameter = float(nozzle)
                    log.info(f"Nozzle diameter from config: {self._nozzle_diameter}mm")
        except Exception as e:
            log.debug(f"Could not read nozzle diameter: {e}")

    # ==================== Status ====================
    
    def get_status(self) -> MoonrakerStatus:
        """Get complete printer status in one call."""
        status = MoonrakerStatus()
        status.device_type = self._device_type
        status.webcam_stream_url = self._webcam_stream
        status.webcam_snapshot_url = self._webcam_snapshot
        
        # Query all objects we care about in one request
        objects = [
            "heater_bed",
            "extruder",
            "print_stats",
            "display_status",
            "idle_timeout",
            "mmu",
            "virtual_sdcard",
            "gcode_move",
            "webhooks",
        ]
        if self._has_fan:
            objects.append("fan")
        objects.extend(self._temperature_sensors)
        objects.extend(self._filament_sensors)
        query = "&".join(objects)
        
        try:
            data = self._get(f"/printer/objects/query?{query}")
            if not data or "result" not in data:
                status.state = MoonrakerState.DISCONNECTED
                status.internal_state = "OFFLINE"
                return status
            
            result = data["result"]["status"]
            status.raw_data = result
            
            # Temperatures
            bed = result.get("heater_bed", {})
            status.bed_temp = bed.get("temperature", 0.0)
            status.bed_target = bed.get("target", 0.0)
            
            extruder = result.get("extruder", {})
            status.nozzle_temp = extruder.get("temperature", 0.0)
            status.nozzle_target = extruder.get("target", 0.0)
            
            # Print stats
            ps = result.get("print_stats", {})
            status.filename = ps.get("filename", "")
            status.print_duration = ps.get("print_duration", 0.0)
            status.filament_used_mm = ps.get("filament_used", 0.0)
            
            layer_info = ps.get("info", {})
            status.current_layer = layer_info.get("current_layer", 0)
            status.total_layers = layer_info.get("total_layer", 0)
            
            # Progress from virtual_sdcard (more reliable than display_status)
            vsd = result.get("virtual_sdcard", {})
            if vsd:
                status.progress_percent = round(vsd.get("progress", 0.0) * 100, 1)
            
            # State mapping
            print_state = ps.get("state", "standby").lower()
            idle_state = result.get("idle_timeout", {}).get("state", "").lower()
            
            if print_state == "printing":
                status.state = MoonrakerState.PRINTING
            elif print_state == "paused":
                status.state = MoonrakerState.PAUSED
            elif print_state == "error":
                status.state = MoonrakerState.ERROR
            elif print_state in ("standby", "complete", "cancelled"):
                status.state = MoonrakerState.READY
            else:
                status.state = MoonrakerState.STANDBY
            
            status.internal_state = MOONRAKER_TO_INTERNAL_STATE.get(
                status.state, "IDLE"
            )
            
            # MMU / ACE filament slots
            mmu = result.get("mmu", {})
            if mmu and mmu.get("enabled"):
                status.filament_slots = self._parse_mmu_slots(mmu)

            # Fan speed (Klipper reports 0.0-1.0, convert to 0-100)
            fan_data = result.get("fan", {})
            if fan_data:
                status.fan_speed = round(fan_data.get("speed", 0.0) * 100)

            # Speed / extrusion factors
            gcode_move = result.get("gcode_move", {})
            if gcode_move:
                status.speed_factor = gcode_move.get("speed_factor", 1.0)
                status.extrude_factor = gcode_move.get("extrude_factor", 1.0)

            # Nozzle diameter (cached on connect)
            status.nozzle_diameter = self._nozzle_diameter

            # Error message from webhooks
            webhooks = result.get("webhooks", {})
            if webhooks:
                status.error_message = webhooks.get("state_message", "")

            # Temperature sensors (chamber + environment)
            for sensor_name in self._temperature_sensors:
                sensor_data = result.get(sensor_name, {})
                if sensor_data:
                    temp = sensor_data.get("temperature")
                    if temp is not None:
                        short_name = sensor_name.split(" ", 1)[1] if " " in sensor_name else sensor_name
                        # First chamber-like sensor becomes chamber_temp
                        if status.chamber_temp is None and any(
                            kw in short_name.lower() for kw in ("chamber", "enclosure")
                        ):
                            status.chamber_temp = round(temp, 1)
                        # All env sensors go into environment_sensors dict
                        if sensor_name in self._env_sensors:
                            status.environment_sensors[short_name] = round(temp, 1)

            # Filament sensor(s)
            for sensor_name in self._filament_sensors:
                sensor_data = result.get(sensor_name, {})
                if sensor_data and "filament_detected" in sensor_data:
                    status.filament_detected = sensor_data["filament_detected"]
                    break  # Use first sensor found

        except Exception as e:
            log.error(f"Failed to get status from {self.host}: {e}")
            status.state = MoonrakerState.DISCONNECTED
            status.internal_state = "OFFLINE"
        
        return status
    
    def _parse_mmu_slots(self, mmu: Dict) -> List[MoonrakerFilamentSlot]:
        """Parse MMU/ACE gate data into filament slots."""
        slots = []
        num_gates = mmu.get("num_gates", 0)
        
        gate_status = mmu.get("gate_status", [])
        gate_material = mmu.get("gate_material", [])
        gate_color = mmu.get("gate_color", [])
        gate_names = mmu.get("gate_filament_name", [])
        gate_temps = mmu.get("gate_temperature", [])
        
        for i in range(num_gates):
            slot = MoonrakerFilamentSlot(
                gate=i,
                material=gate_material[i] if i < len(gate_material) else "",
                color_hex=gate_color[i] if i < len(gate_color) else "",
                loaded=gate_status[i] == 1 if i < len(gate_status) else False,
                name=gate_names[i] if i < len(gate_names) else "",
                temperature=gate_temps[i] if i < len(gate_temps) else 0,
            )
            slots.append(slot)
        
        return slots
    
    # ==================== Job Control ====================
    
    def start_print(self, filename: str) -> bool:
        """Start printing a file already on the printer."""
        try:
            resp = self._post(f"/printer/print/start?filename={filename}")
            return resp is not None
        except Exception as e:
            log.error(f"Failed to start print: {e}")
            return False
    
    def pause_print(self) -> bool:
        """Pause current print."""
        try:
            resp = self._post("/printer/print/pause")
            return resp is not None
        except Exception as e:
            log.error(f"Failed to pause: {e}")
            return False
    
    def resume_print(self) -> bool:
        """Resume paused print."""
        try:
            resp = self._post("/printer/print/resume")
            return resp is not None
        except Exception as e:
            log.error(f"Failed to resume: {e}")
            return False
    
    def cancel_print(self) -> bool:
        """Cancel current print."""
        try:
            resp = self._post("/printer/print/cancel")
            return resp is not None
        except Exception as e:
            log.error(f"Failed to cancel: {e}")
            return False
    
    # ==================== Webcam ====================
    
    def _discover_webcam(self):
        """Discover webcam URLs from Moonraker."""
        try:
            data = self._get("/server/webcams/list")
            if data and "result" in data:
                webcams = data["result"].get("webcams", [])
                if webcams:
                    cam = webcams[0]  # Use first webcam
                    stream = cam.get("stream_url", "")
                    snapshot = cam.get("snapshot_url", "")
                    
                    # Make relative URLs absolute
                    if stream and stream.startswith("/"):
                        stream = f"{self.base_url}{stream}"
                    if snapshot and snapshot.startswith("/"):
                        snapshot = f"{self.base_url}{snapshot}"
                    
                    self._webcam_stream = stream
                    self._webcam_snapshot = snapshot
                    log.info(f"Webcam discovered: {stream}")
        except Exception as e:
            log.warning(f"Webcam discovery failed: {e}")
    
    def get_webcam_urls(self) -> Dict[str, str]:
        """Return cached webcam URLs."""
        return {
            "stream_url": self._webcam_stream,
            "snapshot_url": self._webcam_snapshot,
        }
    
    # ==================== G-code ====================
    
    def send_gcode(self, gcode: str) -> bool:
        """Send a G-code command to the printer."""
        try:
            resp = self._post(f"/printer/gcode/script?script={gcode}")
            return resp is not None
        except Exception as e:
            log.error(f"Failed to send gcode: {e}")
            return False
    
    # ==================== Job History ====================
    
    def get_job_history(self, limit: int = 20) -> List[Dict]:
        """Get print job history from Moonraker."""
        try:
            data = self._get(f"/server/history/list?limit={limit}")
            if data and "result" in data:
                return data["result"].get("jobs", [])
            return []
        except Exception as e:
            log.warning(f"Failed to get job history: {e}")
            return []
    
    # ==================== HTTP Helpers ====================
    
    def _get(self, path: str, timeout: int = 5) -> Optional[Dict]:
        """GET request to Moonraker API."""
        url = f"{self.base_url}{path}"
        try:
            req = Request(url)
            if self.api_key:
                req.add_header("X-Api-Key", self.api_key)
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            log.warning(f"HTTP {e.code} from {url}")
            return None
        except URLError as e:
            log.warning(f"Connection failed to {url}: {e.reason}")
            return None
        except Exception as e:
            log.warning(f"Request failed: {url} - {e}")
            return None
    
    def _post(self, path: str, data: Dict = None, timeout: int = 10) -> Optional[Dict]:
        """POST request to Moonraker API."""
        url = f"{self.base_url}{path}"
        try:
            body = json.dumps(data).encode() if data else b""
            req = Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            if self.api_key:
                req.add_header("X-Api-Key", self.api_key)
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            log.warning(f"HTTP {e.code} from {url}")
            return None
        except URLError as e:
            log.warning(f"Connection failed to {url}: {e.reason}")
            return None
        except Exception as e:
            log.warning(f"POST failed: {url} - {e}")
            return None
