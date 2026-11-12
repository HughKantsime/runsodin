"""
Ultimaker Adapter — local REST API client for Ultimaker S-series and Ultimaker 3 (Experimental).

Part 1: MONITORING AND CAMERA ONLY. Built only from Ultimaker's public local API documentation
(the printer serves it at http://<printer>/docs/api/). NOT verified on hardware.

Endpoints (reads need no credentials): GET /api/v1/system, GET /api/v1/printer,
GET /api/v1/print_job.

Only HTTP GET is ever sent to the printer. There is no write method on this class, no
credential, no pairing and no job sending (Parts 2 and 3).

Architecture note:
  This is the adapter (API client). The monitor (polling daemon) is monitors/ultimaker_monitor.py
  and the pure parsing is parsing/ultimaker.py. Same split as octoprint.

Fail closed:
  * No answer at all (timeout, refused, blocked destination) is DISCONNECTED.
  * Any other unexpected status, malformed or oversized JSON, an unknown state string or a
    contradiction between /printer and /print_job is UNKNOWN (never IDLE, never PRINTING).
  * HTTP 404 on GET /print_job is "no job" (that is how the printer answers when it has none).
"""

import json
import socket
import threading
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

# One pinning policy for every HTTP device adapter: reuse it, never fork it.
from modules.printers.adapters.octoprint import _origin
from modules.printers.adapters.prusalink import _PinnedRequests
from modules.printers.parsing.ultimaker import combine_state, parse_job, parse_printer, parse_system

_http = _PinnedRequests()
log = logging.getLogger(__name__)

API = "/api/v1"
TIMEOUT = 5            # seconds, per request
_MAX_BODY = 65_536     # bytes, per response
_BODY_SECONDS = 10          # whole-body limit, on top of the per-read timeout
CAMERA_PORT = 8080


class UltimakerState(Enum):
    IDLE = "IDLE"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"            # answered, but not something we can classify
    DISCONNECTED = "DISCONNECTED"  # no usable answer (the monitor turns repeats into OFFLINE)


@dataclass
class UltimakerStatus:
    """Parsed status from an Ultimaker printer."""
    state: UltimakerState = UltimakerState.DISCONNECTED
    internal_state: str = "OFFLINE"

    bed_temp: Optional[float] = None
    bed_target: Optional[float] = None
    nozzle_temp: Optional[float] = None     # first extruder
    nozzle_target: Optional[float] = None
    extruders: List[Dict[str, Any]] = field(default_factory=list)  # up to two: temp, target, material_guid

    filename: str = ""
    progress_percent: Optional[float] = None
    time_printing: int = 0       # seconds elapsed
    time_remaining: int = 0      # seconds remaining (total - elapsed); 0 when the total is unknown
    job_uuid: str = ""
    job_state: str = ""          # the printer's own state string, e.g. "printing"
    finished: bool = False       # job state wait_cleanup: the print ended and awaits cleanup
    job_read: bool = False       # GET /print_job answered 200 with an object, or 404 (no job)

    raw_data: Dict[str, Any] = field(default_factory=dict)  # the GET /printer body


def _json_object(reply) -> Optional[dict]:
    """The reply body as a JSON object, read with a hard cap of _MAX_BODY bytes, else None."""
    watchdog = None
    expired = threading.Event()
    try:
        chunks, size = [], 0
        # The request timeout bounds each socket read, not the whole body: a device that
        # dribbles bytes must not hold a monitor thread or an API worker.
        deadline = time.monotonic() + _BODY_SECONDS
        if getattr(reply, 'raw', None) is not None:
            # requests -> urllib3 -> http.client -> BufferedReader -> SocketIO.
            # Fail closed if this transport layout changes: without the socket we cannot
            # interrupt buffering (including chunk framing and compressed bodies).
            sock = reply.raw._fp.fp.raw._sock

            def expire():
                expired.set()
                try:
                    # close() alone can wait on the buffered reader's lock. shutdown()
                    # wakes its pending recv without taking that lock.
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            watchdog = threading.Timer(_BODY_SECONDS, expire)
            watchdog.daemon = True
            watchdog.start()
        for chunk in reply.iter_content(chunk_size=8192):
            size += len(chunk)
            if size > _MAX_BODY or expired.is_set() or time.monotonic() >= deadline:
                return None
            chunks.append(chunk)
        if expired.is_set() or time.monotonic() >= deadline:
            return None
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except Exception:
        return None
    finally:
        if watchdog is not None:
            watchdog.cancel()
            watchdog.join()  # no late shutdown after closing/releasing the response
        close = getattr(reply, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    return value if isinstance(value, dict) else None


def _close(reply) -> None:
    close = getattr(reply, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class UltimakerPrinter:
    """Client for one Ultimaker printer. Stateless: every call is one or two GET requests."""

    def __init__(self, host: str, port: int = 80):
        self.host = host
        self.port = port
        self.base_url = _origin(host, port)
        self.timeout = TIMEOUT

    # ------------------------------------------------------------------
    # transport: GET only
    # ------------------------------------------------------------------
    def _get(self, path: str):
        """One GET to the printer, or None when it cannot or must not be made.

        Needs a valid origin; passes the ITAR destination check; goes through the pinned device
        policy (no redirects, no proxies, no DNS rebinding). No answer (timeout, refused,
        blocked destination) is None."""
        if self.base_url is None:
            return None
        url = self.base_url + API + path
        from core.itar import ItarOutboundBlocked, enforce_request_destination
        try:
            enforce_request_destination(url)
        except ItarOutboundBlocked as exc:
            log.warning("ultimaker: ITAR blocked %s: %s", self.host, exc)
            return None
        try:
            return _http.get(url, headers={"Accept": "application/json"}, timeout=self.timeout, stream=True)
        except requests.exceptions.Timeout:
            log.debug("Ultimaker timeout: %s", self.host)
        except requests.exceptions.RequestException:
            log.debug("Ultimaker connection failed: %s", self.host)
        except Exception as exc:
            log.warning("Ultimaker request error: %s", type(exc).__name__)
        return None

    def _fetch(self, path: str):
        """(status code, JSON object) : (None, None) when unanswered; object None unless 200 with an object."""
        reply = self._get(path)
        if reply is None:
            return None, None
        code = reply.status_code
        if code != 200:
            _close(reply)
            return code, None
        return code, _json_object(reply)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def get_system(self) -> Optional[Dict[str, str]]:
        """GET /system: name, variant, firmware, guid. None unless it looks like an Ultimaker answer."""
        code, body = self._fetch("/system")
        if code != 200 or body is None:
            return None
        info = parse_system(body)
        if not info["variant"] and not info["firmware"]:
            return None
        return info

    def get_status(self) -> UltimakerStatus:
        """GET /printer (status, temperatures) then GET /print_job (job, progress, times)."""
        code, body = self._fetch("/printer")
        if code is None:
            return UltimakerStatus()
        if body is None:
            return UltimakerStatus(state=UltimakerState.UNKNOWN, internal_state="UNKNOWN")
        parsed = parse_printer(body)
        status = UltimakerStatus(
            state=UltimakerState.UNKNOWN, internal_state="UNKNOWN",
            bed_temp=parsed["bed_temp"], bed_target=parsed["bed_target"],
            nozzle_temp=parsed["nozzle_temp"], nozzle_target=parsed["nozzle_target"],
            extruders=parsed["extruders"], raw_data=body,
        )
        printer_state = parsed["state"]
        if printer_state == "UNKNOWN":
            return status  # maintenance, booting or an unrecognised status: no job read, never guessed

        code, job_body = self._fetch("/print_job")
        job = None
        if code == 404:
            status.job_read = True  # the printer's answer when there is no job
        elif code == 200 and job_body is not None:
            job = parse_job(job_body)
            status.job_read = True
            status.filename = job["filename"]
            status.progress_percent = job["progress_percent"]
            status.time_printing = job["time_printing"]
            status.time_remaining = job["time_remaining"]
            status.job_uuid = job["uuid"]
            status.job_state = job["state_raw"]

        if not status.job_read and printer_state != "ERROR":
            return status  # cannot tell whether a job exists: UNKNOWN
        state, finished = combine_state(printer_state, job)
        status.state = UltimakerState(state)
        status.internal_state = state
        status.finished = finished
        return status

    # ------------------------------------------------------------------
    # camera default (no request is made)
    # ------------------------------------------------------------------
    def default_camera_url(self) -> Optional[str]:
        """http://<configured host>:8080/?action=stream. A documented default, never probed.

        The host is the printer's own configured address. Registering it goes through the camera
        discovery policy (same host, device destination policy, shell-safe characters)."""
        if self.base_url is None:
            return None
        host = urlsplit(self.base_url).hostname
        if not host:
            return None
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{CAMERA_PORT}/?action=stream"
