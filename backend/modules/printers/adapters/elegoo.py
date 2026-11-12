"""
Elegoo SDCP Adapter — WebSocket client for Elegoo 3D printers.

Supports: Centauri Carbon (FDM), Neptune 4 series, Saturn series (resin)
Protocol: SDCP v3.0.0 (Smart Device Control Protocol)
Transport: WebSocket (ws://printer_ip:3030/websocket) + UDP discovery (port 3000)  # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- by design — Elegoo SDCP printer protocol uses ws:// on LAN, no TLS in printer firmware
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

import hashlib
import os

from modules.printers.parsing.elegoo import (parse_response, parse_status as parse_elegoo_status,
                                             parse_upload_reply)

log = logging.getLogger(__name__)

# Sources, marked wherever a command id, endpoint or field is used:
#   [repo] already used by this repo's adapter or monitor before job sending existed.
#   [docs] SDCP public documentation (SDCP V3.0.0 repository, docs.opencentauri.cc), unverified on hardware.
UPLOAD_PATH = "/uploadFile/upload"   # [docs] HTTP multipart upload on the SDCP port
UPLOAD_CHUNK_BYTES = 1024 * 1024     # [docs] chunked upload with an Offset per chunk
UPLOAD_TIMEOUT = 60
COMMAND_TIMEOUT = 15.0
GCODE_SUFFIX = ".gcode"
_MAX_NAME = 255
_MAX_BODY = 1_048_576


class ElegooOutcomeUnknown(RuntimeError):
    """A request was sent (or may have been) and no usable answer came back.

    Distinct from a refusal: the printer may have accepted the upload or started the print."""


def sdcp_request_frame(mainboard_id: str, cmd: int, cmd_data: Optional[Dict], request_id: str) -> Dict:
    """One SDCP request frame, shaped exactly as _send_command sends it [repo]."""
    return {
        "Id": str(uuid.uuid4()),
        "Data": {
            "Cmd": cmd,
            "Data": cmd_data or {},
            "RequestID": request_id,
            "MainboardID": mainboard_id,
            "TimeStamp": int(time.time()),
            "From": 0,
        },
        "Topic": f"sdcp/request/{mainboard_id}",
    }


def _usable_name(name: str) -> bool:
    """Only .gcode, a plain file name, no control characters."""
    return (bool(name) and len(name) <= _MAX_NAME and name.lower().endswith(GCODE_SUFFIX)
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def _json_body(reply):
    try:
        if len(reply.content) > _MAX_BODY:
            return None
        return reply.json()
    except Exception:
        return None


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

    Connects to ws://printer_ip:3030/websocket for real-time bidirectional  # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- by design — Elegoo SDCP printer protocol uses ws:// on LAN, no TLS in printer firmware
    communication. Also supports UDP discovery on port 3000.
    """

    def __init__(self, host: str, port: int = 3030, mainboard_id: str = ""):
        self.host = host
        self.port = port
        self.mainboard_id = mainboard_id
        self.ws_url = f"ws://{host}:{port}/websocket"  # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- by design — Elegoo SDCP printer protocol uses ws:// on LAN, no TLS in printer firmware
        self._ws = None
        self._connected = False
        self._latest_status = ElegooStatus()
        self._status_lock = threading.Lock()
        self._transport_lock = threading.RLock()
        self._connection_ready = threading.Event()
        self._on_status_callback: Optional[Callable] = None
        # request_id -> [Event, {"ack": int}, cmd]: the command waiting for its sdcp/response
        self._replies: Dict[str, list] = {}
        self._reply_lock = threading.Lock()
        self.command_timeout = COMMAND_TIMEOUT

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

        ws = None
        try:
            # SDCP has no TLS: connect to the policy-validated IP literal so
            # neither this connect nor a reconnect can be DNS-rebound.
            from core.webhook_utils import device_connect_ip
            address = device_connect_ip(self.host, self.port)
            shown = f"[{address}]" if ":" in address else address
            ws = websocket.WebSocketApp(
                f"ws://{shown}:{self.port}/websocket",  # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- by design — Elegoo SDCP printer protocol uses ws:// on LAN, no TLS in printer firmware
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open,
            )
            ready = threading.Event()
            with self._transport_lock:
                old_ws = self._ws
                self._connection_ready.set()
                self._ws = ws
                self._connected = False
                self._connection_ready = ready
            if old_ws is not None:
                try:
                    old_ws.close()
                except Exception:
                    log.warning("Retired SDCP socket close failed")
            thread = threading.Thread(target=self._run_socket, args=(ws,), daemon=True)
            with self._transport_lock:
                if self._ws is not ws:
                    return False
                self._ws_thread = thread
                thread.start()
            ready.wait(5)
            with self._transport_lock:
                connected = self._ws is ws and self._connected
            if connected:
                return True
            self._retire_socket(ws)
            log.warning("SDCP connection did not become ready")
            return False
        except Exception:
            if ws is not None:
                self._retire_socket(ws)
            log.warning("SDCP connection failed")
            return False

    def _run_socket(self, ws):
        try:
            with self._transport_lock:
                if self._ws is not ws:
                    return
            ws.run_forever(ping_interval=30, ping_timeout=10)
        finally:
            try:
                ws.close()
            except Exception:
                log.warning("SDCP runner socket close failed")

    def _retire_socket(self, expected=None):
        with self._transport_lock:
            if expected is not None and self._ws is not expected:
                return
            ws = self._ws
            self._ws = None
            self._connected = False
            self._connection_ready.set()
        if ws is not None:
            try:
                ws.close()
            except Exception:
                log.warning("SDCP socket close failed")

    def disconnect(self):
        """Retire before closing; late callbacks cannot affect a replacement."""
        self._retire_socket()

    def _on_open(self, ws):
        with self._transport_lock:
            current = ws is not None and ws is self._ws
            if current:
                self._connected = True
                self._connection_ready.set()
        if not current and ws is not None:
            try:
                ws.close()
            except Exception:
                log.warning("Retired SDCP socket close failed")

    def _on_close(self, ws, close_status_code, close_msg):
        with self._transport_lock:
            if ws is None or ws is not self._ws:
                return
            self._connected = False
            self._connection_ready.set()

    def _on_error(self, ws, error):
        with self._transport_lock:
            if ws is None or ws is not self._ws:
                return
            self._connected = False
            self._connection_ready.set()
        log.warning("SDCP transport error")

    def _on_message(self, ws, message):
        """Only the current socket may parse or deliver a status observation."""
        with self._transport_lock:
            if ws is None or ws is not self._ws:
                return
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                return
            topic = data.get("Topic", "")
            if "sdcp/status/" in topic:
                self._parse_status(data)
            elif "sdcp/notice/" in topic:
                log.debug("SDCP notice received")
            elif "sdcp/response/" in topic:
                self._on_response(data)

    def _parse_status(self, data: Dict):
        """Parse sdcp/status message into ElegooStatus."""
        with self._status_lock:
            status = self._latest_status
            parsed = parse_elegoo_status(data)
            if not parsed:
                return
            for name, value in parsed.items():
                if name == "mainboard_id" and not value:
                    continue
                if hasattr(status, name):
                    setattr(status, name, value)

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
        from core.webhook_utils import device_request
        # Common camera endpoints for Elegoo FDM printers
        candidates = [
            f"http://{self.host}:8080/?action=stream",
            f"http://{self.host}:8080/webcam/?action=stream",
        ]
        for url in candidates:
            try:
                resp = device_request("HEAD", url, timeout=3)  # nosemgrep: python.lang.security.audit.insecure-transport.requests.request-with-http.request-with-http -- by design — Elegoo HTTP-only camera streams on LAN
                if resp.status_code == 200:
                    return url
            except Exception as e:
                log.debug("Camera probe failed")
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

    def cancel_print(self) -> bool:
        """Alias of stop_print: the shared control path may ask for either name."""
        return self.stop_print()

    # ------------------------------------------------------------------
    # job sending (Experimental): HTTP upload, then Cmd 128 over the WebSocket
    #
    # Pause, resume, stop and speed above keep their existing send-only behaviour [repo]: they
    # return True once the frame is written. Only start_print waits for the printer's answer.
    # ------------------------------------------------------------------
    def _on_response(self, data: Dict):
        """Hand a sdcp/response frame to the request waiting for that RequestID and command."""
        parsed = parse_response(data)
        if parsed is None:
            return
        with self._reply_lock:
            waiter = self._replies.get(parsed["request_id"])
            if waiter is not None and waiter[2] == parsed["cmd"] and not waiter[0].is_set():
                waiter[1]["ack"] = parsed["ack"]
                waiter[0].set()

    def _request(self, cmd: int, cmd_data: Optional[Dict] = None, timeout: Optional[float] = None) -> Optional[int]:
        """Send one command and wait for its response.

        Returns the Ack [docs]; None when nothing was sent (not connected, no MainboardID); raises
        ElegooOutcomeUnknown when the frame was written (or may have been) and no response with the
        same RequestID and command came back in time."""
        with self._transport_lock:
            ws, connected = self._ws, self._connected
        if not connected or ws is None or not self.mainboard_id:
            return None
        request_id = str(uuid.uuid4())
        waiter = [threading.Event(), {}, int(cmd)]
        with self._reply_lock:
            self._replies[request_id] = waiter
        try:
            frame = sdcp_request_frame(self.mainboard_id, cmd, cmd_data, request_id)
            try:
                ws.send(json.dumps(frame))
            except Exception as exc:
                raise ElegooOutcomeUnknown("SDCP command write failed part-way") from exc
            if not waiter[0].wait(self.command_timeout if timeout is None else timeout):
                raise ElegooOutcomeUnknown("no SDCP response after the command was sent")
            return waiter[1]["ack"]
        finally:
            with self._reply_lock:
                self._replies.pop(request_id, None)

    def start_print(self, filename: str) -> bool:
        """Start a file that is already on the printer: Cmd 128 [repo id].

        Data fields: Filename and StartLayer [docs, SDCP V3.0.0]; Calibration_switch,
        PrintPlatformType and Tlp_Switch [docs, Centauri Carbon community protocol notes], all
        unverified on hardware. Ack 0 means accepted [docs]; any other Ack is a refusal.

        True only on Ack 0. False when the printer refused or nothing was sent (not connected, no
        MainboardID, unusable name): the print did not start. Raises ElegooOutcomeUnknown when the
        command was sent and no matching answer arrived: the printer may have started."""
        name = os.path.basename(str(filename).replace("\\", "/"))
        if not _usable_name(name):
            log.error("Elegoo start refused: only .gcode file names are sent")
            return False
        ack = self._request(SDCPCommand.START_PRINT, {
            "Filename": name, "StartLayer": 0, "Calibration_switch": 0,
            "PrintPlatformType": 0, "Tlp_Switch": 0,
        })
        if ack is None:
            log.error("Elegoo start not sent (%s): not connected or no MainboardID", self.host)
            return False
        if ack != 0:
            log.warning("Elegoo start refused by the printer (Ack %s, %s)", ack, self.host)
            return False
        return True

    def upload_file(self, local_path: str, remote_filename: str = None) -> bool:
        """POST /uploadFile/upload, multipart, in 1 MiB chunks [docs, unverified on hardware].

        Form fields per chunk: Check=1, Offset, Uuid, TotalSize, S-File-MD5 (MD5 of the whole file, a
        transfer checksum the protocol asks for, not a security use) and File. A chunk succeeds only
        on HTTP 200 whose JSON is {"code": "000000", "success": true}.

        False: refused (unusable name, unreadable or empty file, ITAR block, any answered non-success).
        Raises ElegooOutcomeUnknown when a request was sent and got no answer (timeout, connection
        lost). Nothing is started by an upload; Cmd 128 does that."""
        import requests
        from core.itar import ItarOutboundBlocked, enforce_request_destination
        from modules.printers.adapters.octoprint import _origin
        from modules.printers.adapters.prusalink import _PinnedRequests

        raw_name = remote_filename if remote_filename is not None else local_path
        name = os.path.basename(str(raw_name).replace("\\", "/"))
        if not _usable_name(name):
            log.error("Elegoo upload refused: only .gcode file names are sent")
            return False
        origin = _origin(self.host, self.port)
        if origin is None:
            log.error("Elegoo upload refused: unusable printer address")
            return False
        url = origin + UPLOAD_PATH
        try:
            enforce_request_destination(url)
        except ItarOutboundBlocked as exc:
            log.warning("elegoo: ITAR blocked %s: %s", self.host, exc)
            return False
        try:
            total = os.path.getsize(local_path)
            digest = hashlib.md5(usedforsecurity=False)  # protocol transfer checksum, not security
            with open(local_path, "rb") as handle:
                for block in iter(lambda: handle.read(UPLOAD_CHUNK_BYTES), b""):
                    digest.update(block)
        except OSError:
            log.error("Elegoo upload refused: local file unreadable")
            return False
        if total <= 0:
            log.error("Elegoo upload refused: empty file")
            return False
        checksum, upload_id = digest.hexdigest(), uuid.uuid4().hex
        http = _PinnedRequests()
        offset = 0
        try:
            handle = open(local_path, "rb")
        except OSError:
            log.error("Elegoo upload refused: local file unreadable")
            return False
        with handle:
            while offset < total:
                chunk = handle.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    log.error("Elegoo upload refused: file changed while reading")
                    return False
                try:
                    reply = http.post(
                        url,
                        files={"File": (name, chunk, "application/octet-stream")},
                        data={"Check": "1", "Offset": str(offset), "Uuid": upload_id,
                              "TotalSize": str(total), "S-File-MD5": checksum},
                        timeout=UPLOAD_TIMEOUT,
                    )
                except requests.exceptions.RequestException as exc:
                    raise ElegooOutcomeUnknown("no answer to an upload request") from exc
                except Exception as exc:
                    raise ElegooOutcomeUnknown("upload request failed after it was sent") from exc
                if reply.status_code != 200 or not parse_upload_reply(_json_body(reply)):
                    log.error("Elegoo upload not confirmed (%s, HTTP %s, offset %s)", self.host, reply.status_code, offset)
                    return False
                offset += len(chunk)
        log.info("Uploaded %s to Elegoo at %s", name, self.host)
        return True
