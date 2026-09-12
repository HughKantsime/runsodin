"""Mechanically read-only transports for EDU hardware certification.

This module intentionally does not import ODIN's mixed read/write adapters.
Its public transports expose only the operations allowed by the certification
policy, and every mutating operation raises before reaching the network.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


class CertificationMutationBlocked(RuntimeError):
    pass


MOONRAKER_PATHS = {
    "/server/info",
    "/printer/info",
    "/printer/objects/list",
    "/printer/objects/query",
}
MOONRAKER_OBJECTS = {
    "heater_bed",
    "extruder",
    "print_stats",
    "display_status",
    "idle_timeout",
    "virtual_sdcard",
    "gcode_move",
    "webhooks",
    "fan",
}
PRUSALINK_PATHS = {"/api/version", "/api/v1/status", "/api/printer", "/api/job"}


@dataclass(frozen=True)
class ReadOnlyHttpTransport:
    base_url: str
    protocol: str
    timeout: float = 5.0

    def _validate(self, path: str) -> None:
        base = urlsplit(self.base_url)
        if base.scheme not in {"http", "https"} or not base.hostname:
            raise CertificationMutationBlocked("Certification base URL must be HTTP(S)")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise CertificationMutationBlocked("Certification paths must be relative")
        if self.protocol == "moonraker":
            if parsed.path not in MOONRAKER_PATHS:
                raise CertificationMutationBlocked("Moonraker path is not certification-allowlisted")
            if parsed.path != "/printer/objects/query" and parsed.query:
                raise CertificationMutationBlocked("Query parameters are not allowed on this endpoint")
            if parsed.path == "/printer/objects/query":
                objects = {item for item in parsed.query.split("&") if item}
                if not objects or not objects <= MOONRAKER_OBJECTS:
                    raise CertificationMutationBlocked("Moonraker object query is not allowlisted")
        elif self.protocol == "prusalink":
            if parsed.path not in PRUSALINK_PATHS or parsed.query:
                raise CertificationMutationBlocked("PrusaLink path is not certification-allowlisted")
        else:
            raise CertificationMutationBlocked("HTTP certification supports Moonraker or PrusaLink only")

    def get(self, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self._validate(path)
        request = urllib.request.Request(self.base_url.rstrip("/") + path, method="GET")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- HTTP(S) base and exact path are validated above
            return json.loads(response.read().decode("utf-8"))

    def _deny(self, *_args, **_kwargs):
        raise CertificationMutationBlocked("Mutating HTTP methods are disabled in certification mode")

    post = put = patch = delete = head = _deny


class PassiveMqttTransport:
    """Narrow wrapper: connect, subscribe to one report topic, receive, close."""

    def __init__(self, client: Any, redacted_device_token: str):
        self.__client = client
        self.__topic = f"device/{redacted_device_token}/report"

    def connect(self, host: str, port: int, keepalive: int = 30):
        return self.__client.connect(host, port, keepalive=keepalive)

    def subscribe(self, topic: str):
        if topic != self.__topic:
            raise CertificationMutationBlocked("Only the configured Bambu report topic is allowed")
        return self.__client.subscribe(topic, qos=0)

    def disconnect(self):
        return self.__client.disconnect()

    def publish(self, *_args, **_kwargs):
        raise CertificationMutationBlocked("MQTT publish is disabled in certification mode")


class PassiveWebSocketTransport:
    """Narrow wrapper for receiving unsolicited Elegoo SDCP frames."""

    def __init__(self, websocket: Any):
        self.__websocket = websocket

    def receive(self):
        return self.__websocket.recv()

    def close(self):
        return self.__websocket.close()

    def send(self, *_args, **_kwargs):
        raise CertificationMutationBlocked("WebSocket send is disabled in certification mode")

    send_text = send_bytes = send_json = send


def redact_observation(protocol: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return capability/version evidence without identity, host, or job content."""
    allowed = {
        "protocol",
        "model_family",
        "firmware_version",
        "api_version",
        "capabilities",
        "latency_ms",
        "freshness_seconds",
        "status",
        "reason",
    }
    redacted = {key: value for key, value in payload.items() if key in allowed}
    redacted["protocol"] = protocol
    return redacted
