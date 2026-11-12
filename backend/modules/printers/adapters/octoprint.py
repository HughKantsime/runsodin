"""
OctoPrint Adapter — REST API client for OctoPrint servers (Experimental).

Built only from OctoPrint's public REST API documentation (docs.octoprint.org). OctoPrint
itself is AGPL: none of its code is used or copied here.

Auth: X-Api-Key (an application key the user creates in OctoPrint)
Endpoints: GET /api/version, GET /api/printer, GET /api/job, GET /api/settings,
           POST /api/files/local (multipart), POST /api/job

Architecture note:
  This is the adapter (API client). The monitor (polling daemon) is monitors/octoprint_monitor.py
  and the pure parsing is parsing/octoprint.py. Same split as prusalink.

Fail closed:
  * HTTP 409 on GET /api/printer ("printer is not operational") is OFFLINE.
  * Any other unexpected status, malformed JSON or unrecognized flags is UNKNOWN
    (never IDLE, never PRINTING).
  * No answer at all (timeout, refused, blocked destination) is DISCONNECTED.
  * Upload succeeds only on 201 with done=true; pause, resume and cancel only on 204.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import requests

# One pinning policy for every HTTP device adapter: reuse it, never fork it.
from modules.printers.adapters.prusalink import _PinnedRequests
from modules.printers.parsing.octoprint import parse_job, parse_printer

_http = _PinnedRequests()
log = logging.getLogger(__name__)

_MAX_BODY = 1_048_576
_MAX_NAME = 255
_KEY = re.compile(r"[\x21-\x7e]+")


class OctoPrintState(Enum):
    IDLE = "IDLE"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"            # server answered 409: the printer is not operational
    UNKNOWN = "UNKNOWN"            # answered, but not something we can classify
    DISCONNECTED = "DISCONNECTED"  # no usable answer


@dataclass
class OctoPrintStatus:
    """Parsed status from an OctoPrint server."""
    state: OctoPrintState = OctoPrintState.DISCONNECTED
    internal_state: str = "OFFLINE"

    bed_temp: Optional[float] = None
    bed_target: Optional[float] = None
    nozzle_temp: Optional[float] = None
    nozzle_target: Optional[float] = None

    filename: str = ""
    progress_percent: Optional[float] = None
    time_printing: int = 0       # seconds elapsed
    time_remaining: int = 0      # seconds remaining (as OctoPrint estimates)
    current_layer: int = 0
    total_layers: int = 0
    job_state: str = ""

    raw_data: Dict[str, Any] = field(default_factory=dict)  # the GET /api/printer body


def _origin(host: object, port: object) -> Optional[str]:
    """http://host:port from a configured host (optionally host:port or [v6]:port), else None."""
    raw = str(host or "").strip()
    if not raw or any(ord(c) <= 32 or ord(c) == 127 for c in raw):
        return None
    if raw.count(":") > 1 and not raw.startswith("["):
        raw = f"[{raw}]"
    try:
        parts = urlsplit("//" + raw)
        hostname, parsed_port = parts.hostname, parts.port
    except ValueError:
        return None
    if not hostname or parts.username is not None or parts.password is not None \
            or parts.path or parts.query or parts.fragment:
        return None
    use_port = parsed_port if parsed_port is not None else port
    if type(use_port) is not int or not 1 <= use_port <= 65535:
        return None
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"http://{hostname}:{use_port}"


def _json_object(reply) -> Optional[dict]:
    try:
        if len(reply.content) > _MAX_BODY:
            return None
        value = reply.json()
    except Exception:
        return None
    return value if isinstance(value, dict) else None


class OctoPrintPrinter:
    """Client for one OctoPrint server. Stateless: every call is one HTTP request."""

    def __init__(self, host: str, port: int = 80, api_key: str = ""):
        self.host = host
        self.port = port
        self.api_key = api_key if isinstance(api_key, str) and _KEY.fullmatch(api_key) else ""
        self.base_url = _origin(host, port)
        self.timeout = 10

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _call(self, verb: str, path: str, **kwargs):
        """One device request, or None when it cannot or must not be made.

        Needs a key and a valid origin; passes the ITAR destination check; goes through the
        pinned device policy (no redirects, no proxies, no DNS rebinding)."""
        if self.base_url is None or not self.api_key:
            return None
        url = self.base_url + path
        from core.itar import ItarOutboundBlocked, enforce_request_destination
        try:
            enforce_request_destination(url)
        except ItarOutboundBlocked as exc:
            log.warning("octoprint: ITAR blocked %s: %s", self.host, exc)
            return None
        kwargs.setdefault("timeout", self.timeout)
        try:
            return getattr(_http, verb)(url, headers={"X-Api-Key": self.api_key}, **kwargs)
        except requests.exceptions.Timeout:
            log.debug("OctoPrint timeout: %s", self.host)
        except requests.exceptions.RequestException:
            log.debug("OctoPrint connection failed: %s", self.host)
        except Exception as exc:
            log.warning("OctoPrint request error: %s", type(exc).__name__)
        return None

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def get_version(self) -> Optional[Dict]:
        """GET /api/version: server identity and version. Never used to guess a printer model."""
        reply = self._call("get", "/api/version")
        if reply is None or reply.status_code != 200:
            return None
        return _json_object(reply)

    def get_status(self) -> OctoPrintStatus:
        """GET /api/printer (state flags, temperatures) then GET /api/job (file, progress)."""
        reply = self._call("get", "/api/printer")
        if reply is None:
            return OctoPrintStatus()
        if reply.status_code == 409:
            return OctoPrintStatus(state=OctoPrintState.OFFLINE, internal_state="OFFLINE")
        body = _json_object(reply) if reply.status_code == 200 else None
        if body is None:
            if reply.status_code in (401, 403):
                log.warning("OctoPrint rejected the application key for %s", self.host)
            return OctoPrintStatus(state=OctoPrintState.UNKNOWN, internal_state="UNKNOWN")
        parsed = parse_printer(body)
        status = OctoPrintStatus(
            state=OctoPrintState(parsed["state"]), internal_state=parsed["state"],
            bed_temp=parsed["bed_temp"], bed_target=parsed["bed_target"],
            nozzle_temp=parsed["nozzle_temp"], nozzle_target=parsed["nozzle_target"],
            raw_data=body,
        )
        if status.state is not OctoPrintState.UNKNOWN:
            self._apply_job(status)
        return status

    def _apply_job(self, status: OctoPrintStatus) -> None:
        reply = self._call("get", "/api/job")
        if reply is None or reply.status_code != 200:
            return
        body = _json_object(reply)
        if body is None:
            return
        job = parse_job(body)
        status.filename = job["filename"]
        status.progress_percent = job["progress_percent"]
        status.time_printing = job["time_printing"]
        status.time_remaining = job["time_remaining"]
        status.job_state = job["job_state"]

    def get_webcam_url(self) -> Optional[str]:
        """The stream URL OctoPrint itself reports in GET /api/settings (webcam.streamUrl).

        Only an absolute http(s) URL with a host and no credentials is returned. A relative
        path is not resolved against the configured address (that would be inventing a URL),
        and nothing is probed when the setting is absent."""
        reply = self._call("get", "/api/settings")
        if reply is None or reply.status_code != 200:
            return None
        body = _json_object(reply)
        webcam = body.get("webcam") if body else None
        url = webcam.get("streamUrl") if isinstance(webcam, dict) else None
        if not isinstance(url, str) or not url or len(url) > 2048:
            return None
        if any(ord(c) <= 32 or ord(c) == 127 for c in url):
            return None
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
            parts.port  # validates the authority
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or not hostname or parts.username is not None \
                or parts.password is not None:
            return None
        return url

    # ------------------------------------------------------------------
    # job sending
    # ------------------------------------------------------------------
    def upload_and_print(self, local_path: str, remote_filename: str = None) -> bool:
        """POST /api/files/local (multipart file, select=true, print=true): upload and start.

        Success only on HTTP 201 whose JSON says done=true. Anything else (400, 409, 415,
        a malformed body, a timeout) is a refusal."""
        raw_name = remote_filename if remote_filename is not None else local_path
        name = os.path.basename(str(raw_name).replace("\\", "/"))
        if not name or len(name) > _MAX_NAME or any(ord(c) < 32 or ord(c) == 127 for c in name):
            log.error("OctoPrint upload refused: unusable file name")
            return False
        try:
            with open(local_path, "rb") as handle:
                reply = self._call(
                    "post", "/api/files/local",
                    files={"file": (name, handle, "application/octet-stream")},
                    data={"select": "true", "print": "true"},
                    timeout=120,
                )
        except OSError:
            log.error("OctoPrint upload refused: local file unreadable")
            return False
        if reply is None:
            log.error("OctoPrint upload failed (%s): no answer", self.host)
            return False
        if reply.status_code != 201:
            log.error("OctoPrint upload returned %s (%s)", reply.status_code, self.host)
            return False
        body = _json_object(reply)
        if body is None or body.get("done") is not True:
            log.error("OctoPrint upload not confirmed (%s): 201 without done=true", self.host)
            return False
        log.info("Uploaded and started %s on OctoPrint at %s", name, self.host)
        return True

    # ------------------------------------------------------------------
    # controls: POST /api/job, success only on 204
    # ------------------------------------------------------------------
    def _job_command(self, payload: dict) -> bool:
        reply = self._call("post", "/api/job", json=payload)
        if reply is None:
            return False
        if reply.status_code == 204:
            return True
        log.warning("OctoPrint job command %s returned %s", payload.get("command"), reply.status_code)
        return False

    def pause_print(self) -> bool:
        return self._job_command({"command": "pause", "action": "pause"})

    def resume_print(self) -> bool:
        return self._job_command({"command": "pause", "action": "resume"})

    def stop_print(self) -> bool:
        return self._job_command({"command": "cancel"})

    def cancel_print(self) -> bool:
        return self.stop_print()
