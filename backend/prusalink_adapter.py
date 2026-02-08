"""
PrusaLink Adapter — REST API client for Prusa 3D printers.

Supports: MK4/S, MK3.9, MK3.5, MINI+, XL, CORE One
Protocol: PrusaLink REST API (v1)
Auth: HTTP Digest (username + password) or API key header
Endpoints: /api/v1/status (combined printer + job), /api/printer, /api/job, /api/version

Reference:
  - OpenAPI spec: https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml
  - PrusaLinkPy: https://pypi.org/project/PrusaLinkPy/

Architecture note:
  This is the adapter (API client). The monitor (polling daemon) is prusalink_monitor.py.
  Same split as moonraker_adapter.py / moonraker_monitor.py.
"""

import logging
import requests
from requests.auth import HTTPDigestAuth
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from enum import Enum

log = logging.getLogger(__name__)


class PrusaLinkState(Enum):
    IDLE = "IDLE"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    ATTENTION = "ATTENTION"
    BUSY = "BUSY"
    ERROR = "ERROR"
    FINISHED = "FINISHED"
    STOPPED = "STOPPED"
    DISCONNECTED = "DISCONNECTED"


@dataclass
class PrusaLinkStatus:
    """Parsed status from a PrusaLink printer."""
    state: PrusaLinkState = PrusaLinkState.DISCONNECTED
    internal_state: str = "OFFLINE"

    # Temperatures
    bed_temp: float = 0.0
    bed_target: float = 0.0
    nozzle_temp: float = 0.0
    nozzle_target: float = 0.0

    # Print progress
    filename: str = ""
    progress_percent: float = 0.0
    time_printing: int = 0         # seconds elapsed
    time_remaining: int = 0        # seconds remaining (provided by printer!)
    current_layer: int = 0
    total_layers: int = 0

    # Printer info
    device_type: str = ""
    nozzle_diameter: float = 0.4
    axis_z: float = 0.0
    flow: int = 100
    speed: int = 100
    fan_hotend: int = 0
    fan_print: int = 0

    # Job info
    job_id: Optional[int] = None

    # Raw data for debugging
    raw_data: Dict[str, Any] = field(default_factory=dict)


class PrusaLinkPrinter:
    """
    Client for PrusaLink REST API.

    Handles all communication with a single PrusaLink-based printer.
    No persistent connection needed — each call is a simple HTTP request.
    """

    def __init__(self, host: str, port: int = 80, username: str = "maker",
                 password: str = "", api_key: str = ""):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.api_key = api_key
        self.base_url = f"http://{host}:{port}"
        self.timeout = 10

    def _get(self, path: str) -> Optional[Dict]:
        """Make authenticated GET request to PrusaLink."""
        url = f"{self.base_url}{path}"
        headers = {}

        try:
            if self.api_key:
                # API key auth (X-Api-Key header)
                headers["X-Api-Key"] = self.api_key
                resp = requests.get(url, headers=headers, timeout=self.timeout)
            else:
                # HTTP Digest auth (username + password)
                resp = requests.get(
                    url,
                    auth=HTTPDigestAuth(self.username, self.password),
                    timeout=self.timeout
                )

            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 204:
                return {}  # No content (e.g., no active job)
            elif resp.status_code == 401:
                log.warning(f"PrusaLink auth failed for {self.host}")
                return None
            else:
                log.warning(f"PrusaLink {path} returned {resp.status_code}")
                return None

        except requests.exceptions.Timeout:
            log.debug(f"PrusaLink timeout: {self.host}")
            return None
        except requests.exceptions.ConnectionError:
            log.debug(f"PrusaLink connection failed: {self.host}")
            return None
        except Exception as e:
            log.warning(f"PrusaLink request error: {e}")
            return None

    def get_version(self) -> Optional[Dict]:
        """GET /api/version — firmware and API version info."""
        return self._get("/api/version")

    def get_status(self) -> PrusaLinkStatus:
        """
        GET /api/v1/status — combined printer + job status.
        This is the primary polling endpoint (available on newer firmware).

        Response format:
        {
            "job": {"id": 297, "progress": 91.0, "time_remaining": 600, "time_printing": 7718},
            "storage": {"path": "/usb/", "name": "usb", "read_only": false},
            "printer": {
                "state": "PRINTING",
                "temp_bed": 60.0, "target_bed": 60.0,
                "temp_nozzle": 209.9, "target_nozzle": 210.0,
                "axis_z": 2.4, "flow": 100, "speed": 100,
                "fan_hotend": 3099, "fan_print": 5964
            }
        }
        """
        status = PrusaLinkStatus()

        # Try v1/status first (newer firmware, single request)
        data = self._get("/api/v1/status")

        if data is None:
            # Fallback to legacy endpoints
            return self._get_status_legacy()

        status.raw_data = data

        # Parse printer section
        printer = data.get("printer", {})
        status.bed_temp = printer.get("temp_bed", 0.0)
        status.bed_target = printer.get("target_bed", 0.0)
        status.nozzle_temp = printer.get("temp_nozzle", 0.0)
        status.nozzle_target = printer.get("target_nozzle", 0.0)
        status.axis_z = printer.get("axis_z", 0.0)
        status.flow = printer.get("flow", 100)
        status.speed = printer.get("speed", 100)
        status.fan_hotend = printer.get("fan_hotend", 0)
        status.fan_print = printer.get("fan_print", 0)

        # State mapping
        printer_state = printer.get("state", "IDLE").upper()
        status.internal_state = printer_state
        status.state = self._map_state(printer_state)

        # Parse job section
        job = data.get("job", {})
        if job:
            status.job_id = job.get("id")
            status.progress_percent = job.get("progress", 0.0)
            status.time_printing = job.get("time_printing", 0)
            status.time_remaining = job.get("time_remaining", 0)  # PrusaLink provides this!

        return status

    def _get_status_legacy(self) -> PrusaLinkStatus:
        """
        Fallback: use /api/printer + /api/job (OctoPrint-compatible endpoints).
        Older PrusaLink firmware may not have /api/v1/status.
        """
        status = PrusaLinkStatus()

        # GET /api/printer
        printer_data = self._get("/api/printer")
        if printer_data is None:
            status.state = PrusaLinkState.DISCONNECTED
            status.internal_state = "OFFLINE"
            return status

        status.raw_data = printer_data

        # Temperature
        temp = printer_data.get("temperature", {})
        tool0 = temp.get("tool0", {})
        bed = temp.get("bed", {})
        status.nozzle_temp = tool0.get("actual", 0.0)
        status.nozzle_target = tool0.get("target", 0.0)
        status.bed_temp = bed.get("actual", 0.0)
        status.bed_target = bed.get("target", 0.0)

        # State from flags
        state_info = printer_data.get("state", {})
        flags = state_info.get("flags", {})
        if flags.get("printing"):
            status.state = PrusaLinkState.PRINTING
            status.internal_state = "PRINTING"
        elif flags.get("paused") or flags.get("pausing"):
            status.state = PrusaLinkState.PAUSED
            status.internal_state = "PAUSED"
        elif flags.get("error") or flags.get("closedOnError"):
            status.state = PrusaLinkState.ERROR
            status.internal_state = "ERROR"
        elif flags.get("ready") or flags.get("operational"):
            status.state = PrusaLinkState.IDLE
            status.internal_state = "IDLE"

        # Telemetry (additional data in some firmware versions)
        telemetry = printer_data.get("telemetry", {})
        if telemetry:
            status.axis_z = telemetry.get("z-height", 0.0)

        # GET /api/job
        job_data = self._get("/api/job")
        if job_data:
            job = job_data.get("job", {})
            progress = job_data.get("progress", {})
            status.progress_percent = progress.get("completion", 0.0) or 0.0
            status.time_printing = progress.get("printTime", 0) or 0
            status.time_remaining = progress.get("printTimeLeft", 0) or 0
            if job:
                file_info = job.get("file", {})
                status.filename = file_info.get("display", "") or file_info.get("name", "")

        return status

    def _map_state(self, state_str: str) -> PrusaLinkState:
        """Map PrusaLink state string to enum."""
        mapping = {
            "IDLE": PrusaLinkState.IDLE,
            "READY": PrusaLinkState.IDLE,
            "OPERATIONAL": PrusaLinkState.IDLE,
            "PRINTING": PrusaLinkState.PRINTING,
            "PAUSED": PrusaLinkState.PAUSED,
            "ATTENTION": PrusaLinkState.ATTENTION,
            "BUSY": PrusaLinkState.BUSY,
            "ERROR": PrusaLinkState.ERROR,
            "FINISHED": PrusaLinkState.FINISHED,
            "STOPPED": PrusaLinkState.STOPPED,
        }
        return mapping.get(state_str, PrusaLinkState.IDLE)

    def pause_print(self, job_id: int) -> bool:
        """PUT /api/v1/job/{id}/pause — pause current print."""
        try:
            headers = {}
            kwargs = {}
            if self.api_key:
                headers["X-Api-Key"] = self.api_key
            else:
                kwargs["auth"] = HTTPDigestAuth(self.username, self.password)

            resp = requests.put(
                f"{self.base_url}/api/v1/job/{job_id}/pause",
                headers=headers, timeout=self.timeout, **kwargs
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            log.warning(f"PrusaLink pause failed: {e}")
            return False

    def resume_print(self, job_id: int) -> bool:
        """PUT /api/v1/job/{id}/resume — resume paused print."""
        try:
            headers = {}
            kwargs = {}
            if self.api_key:
                headers["X-Api-Key"] = self.api_key
            else:
                kwargs["auth"] = HTTPDigestAuth(self.username, self.password)

            resp = requests.put(
                f"{self.base_url}/api/v1/job/{job_id}/resume",
                headers=headers, timeout=self.timeout, **kwargs
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            log.warning(f"PrusaLink resume failed: {e}")
            return False

    def stop_print(self, job_id: int) -> bool:
        """DELETE /api/v1/job/{id} — stop/cancel current print."""
        try:
            headers = {}
            kwargs = {}
            if self.api_key:
                headers["X-Api-Key"] = self.api_key
            else:
                kwargs["auth"] = HTTPDigestAuth(self.username, self.password)

            resp = requests.delete(
                f"{self.base_url}/api/v1/job/{job_id}",
                headers=headers, timeout=self.timeout, **kwargs
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            log.warning(f"PrusaLink stop failed: {e}")
            return False
