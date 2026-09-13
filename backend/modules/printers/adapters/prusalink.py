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

from modules.printers.parsing.prusalink import map_state, parse_legacy_status, parse_v1_status

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
    UNKNOWN = "UNKNOWN"
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

        # v1.8.9 (codex pass 19): ITAR guard. PrusaLink is LAN-local
        # by convention but an admin can configure any host; block
        # public destinations under ITAR.
        try:
            from core.itar import enforce_request_destination, ItarOutboundBlocked
            enforce_request_destination(url)
        except ItarOutboundBlocked as exc:
            log.warning("prusalink: ITAR blocked %s: %s", url, exc)
            return None

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

        self._apply_parsed(status, parse_v1_status(data))
        return status

    def _get_status_legacy(self) -> PrusaLinkStatus:
        """
        Fallback: use /api/printer + /api/job (OctoPrint-compatible endpoints).
        Older PrusaLink firmware may not have /api/v1/status.
        """
        printer_data = self._get("/api/printer")
        job_data = self._get("/api/job") if printer_data is not None else None
        status = PrusaLinkStatus()
        self._apply_parsed(status, parse_legacy_status(printer_data, job_data))
        return status

    @staticmethod
    def _apply_parsed(status: PrusaLinkStatus, parsed: Dict[str, Any]) -> None:
        for name, value in parsed.items():
            if name == "state":
                status.state = PrusaLinkState(value)
            elif hasattr(status, name):
                setattr(status, name, value)

    def _map_state(self, state_str: str) -> PrusaLinkState:
        """Map PrusaLink state string to enum."""
        return PrusaLinkState(map_state(state_str))

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

    def get_webcam_url(self) -> Optional[str]:
        """Discover camera snapshot URL from PrusaLink.

        Prusa printers expose a snapshot endpoint. Newer firmware has
        /api/v1/cameras; older firmware uses the OctoPrint-compatible
        /webcam/?action=snapshot path.
        """
        # Try /api/v1/cameras first (newer firmware)
        try:
            data = self._get("/api/v1/cameras")
            if data and isinstance(data, list) and data:
                cam = data[0]
                # The camera config contains resolution info; the actual
                # snapshot/stream is at a fixed path
                return f"{self.base_url}/api/v1/cameras/snap"
        except Exception as e:
            log.debug(f"Camera API probe failed: {e}")

        # Fallback: try the snapshot endpoint directly
        try:
            url = f"{self.base_url}/webcam/?action=snapshot"
            headers = {}
            kwargs = {"timeout": 5}
            if self.api_key:
                headers["X-Api-Key"] = self.api_key
            else:
                kwargs["auth"] = HTTPDigestAuth(self.username, self.password)
            resp = requests.head(url, headers=headers, **kwargs)
            if resp.status_code == 200:
                return url
        except Exception as e:
            log.debug(f"Camera snapshot probe failed: {e}")

        return None

    def upload_and_print(self, local_path: str, remote_filename: str = None) -> bool:
        """Upload a .gcode/.bgcode file to PrusaLink and start printing immediately.

        Uses the OctoPrint-compatible POST /api/files/local endpoint with
        print=true, which uploads the file and kicks off the print in one call.

        Args:
            local_path: Absolute path to the local .gcode or .bgcode file.
            remote_filename: Name to store on the printer (default: basename).

        Returns:
            True if upload and print start succeeded.
        """
        import os as _os
        if remote_filename is None:
            remote_filename = _os.path.basename(local_path)

        url = f"{self.base_url}/api/files/local"
        headers = {}
        kwargs: dict = {"timeout": 120}

        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        else:
            kwargs["auth"] = HTTPDigestAuth(self.username, self.password)

        try:
            with open(local_path, "rb") as f:
                resp = requests.post(
                    url,
                    files={"file": (remote_filename, f, "application/octet-stream")},
                    data={"select": "true", "print": "true"},
                    headers=headers,
                    **kwargs,
                )
            if resp.status_code in (200, 201):
                log.info(f"Uploaded and started {remote_filename} on PrusaLink at {self.host}")
                return True
            log.error(f"PrusaLink upload returned {resp.status_code}: {resp.text[:200]}")
            return False
        except Exception as e:
            log.error(f"PrusaLink upload failed ({self.host}): {e}")
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
