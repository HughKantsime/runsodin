"""Capability-only transports for passive hardware observation."""

from __future__ import annotations

import json
import http.client
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth


class PassivePolicyError(RuntimeError):
    """Raised before transmission when passive policy is violated."""


MAX_JSON_NESTING = 32


def _preflight_json_nesting(payload: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in payload:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING:
                raise PassivePolicyError("passive response exceeds JSON nesting limit")
        elif character in "]}":
            depth -= 1


def decode_bounded_json_object(payload: bytes | str | dict[str, Any]) -> dict[str, Any]:
    """Decode a bounded JSON object and reject pathological nesting before parsing it."""
    try:
        if isinstance(payload, dict):
            loaded: Any = payload
        else:
            text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            if not isinstance(text, str):
                raise TypeError
            _preflight_json_nesting(text)
            loaded = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError) as exc:
        raise PassivePolicyError("passive response JSON is malformed") from exc
    if not isinstance(loaded, dict):
        raise PassivePolicyError("passive response must be a JSON object")
    stack: list[tuple[Any, int]] = [(loaded, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_JSON_NESTING:
            raise PassivePolicyError("passive response exceeds JSON nesting limit")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
    return loaded


MOONRAKER_PATHS = frozenset({
    "/server/info", "/printer/info", "/printer/objects/list", "/printer/objects/query",
})
MOONRAKER_OBJECTS = frozenset({
    "heater_bed", "extruder", "print_stats", "display_status", "idle_timeout",
    "virtual_sdcard", "gcode_move", "webhooks", "fan",
})
PRUSALINK_PATHS = frozenset({"/api/version", "/api/v1/status", "/api/printer", "/api/job"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PassivePolicyError("redirects are disabled for passive certification")


def _open_no_redirect(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


@dataclass(frozen=True)
class ReadOnlyHttpTransport:
    base_url: str
    protocol: str
    timeout: float = 5.0
    max_response_bytes: int = 262_144

    def _validate(self, path: str) -> None:
        base = urlsplit(self.base_url)
        if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password:
            raise PassivePolicyError("certification base URL must be credential-free HTTP(S)")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise PassivePolicyError("certification paths must be relative")
        if self.protocol == "moonraker":
            if parsed.path not in MOONRAKER_PATHS:
                raise PassivePolicyError("Moonraker path is not certification-allowlisted")
            if parsed.path != "/printer/objects/query" and parsed.query:
                raise PassivePolicyError("query parameters are not allowed")
            if parsed.path == "/printer/objects/query":
                objects = {item for item in parsed.query.split("&") if item}
                if not objects or not objects <= MOONRAKER_OBJECTS:
                    raise PassivePolicyError("Moonraker object query is not allowlisted")
        elif self.protocol == "prusalink":
            if parsed.path not in PRUSALINK_PATHS or parsed.query:
                raise PassivePolicyError("PrusaLink path is not certification-allowlisted")
        else:
            raise PassivePolicyError("HTTP protocol is not certification-allowlisted")

    def get(self, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self._validate(path)
        request = urllib.request.Request(self.base_url.rstrip("/") + path, method="GET")
        for name, value in (headers or {}).items():
            if name.lower() not in {"x-api-key", "authorization", "accept"}:
                raise PassivePolicyError("HTTP header is not certification-allowlisted")
            request.add_header(name, value)
        with _open_no_redirect(request, self.timeout) as response:  # nosec B310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- exact scheme, host and path policy above
            content_type = response.headers.get_content_type()
            if content_type not in {"application/json", "text/json"}:
                raise PassivePolicyError("passive response must be JSON")
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise PassivePolicyError("passive response exceeds size limit")
            return decode_bounded_json_object(raw.decode("utf-8"))

@dataclass(frozen=True)
class PinnedReadOnlyHttpTransport:
    """GET-only HTTP(S) transport whose TCP peer is one prevalidated address."""

    host: str
    address: str
    port: int
    protocol: str
    use_tls: bool = False
    timeout: float = 5.0
    max_response_bytes: int = 262_144

    def _policy(self, path: str) -> None:
        ReadOnlyHttpTransport(
            f"{'https' if self.use_tls else 'http'}://{self.host}:{self.port}",
            self.protocol, self.timeout, self.max_response_bytes,
        )._validate(path)

    def __connection(self):
        connection_type = http.client.HTTPSConnection if self.use_tls else http.client.HTTPConnection
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.use_tls:
            kwargs["context"] = ssl.create_default_context()
        connection = connection_type(self.host, self.port, **kwargs)
        connection._create_connection = lambda _target, timeout=None, source_address=None: socket.create_connection(  # type: ignore[attr-defined]
            (self.address, self.port), timeout if timeout is not None else self.timeout, source_address
        )
        return connection

    def get(self, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self._policy(path)
        request_headers = {"Accept": "application/json", "Host": self.host}
        for name, value in (headers or {}).items():
            if name.lower() not in {"x-api-key", "authorization", "accept"}:
                raise PassivePolicyError("HTTP header is not certification-allowlisted")
            request_headers[name] = value
        connection = self.__connection()
        try:
            connection.request("GET", path, headers=request_headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise PassivePolicyError("redirects are disabled for passive certification")
            if response.status < 200 or response.status >= 300:
                raise PassivePolicyError("passive endpoint returned an error status")
            if response.headers.get_content_type() not in {"application/json", "text/json"}:
                raise PassivePolicyError("passive response must be JSON")
            if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise PassivePolicyError("compressed passive responses are disabled")
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise PassivePolicyError("passive response exceeds size limit")
            return decode_bounded_json_object(raw.decode("utf-8"))
        finally:
            connection.close()

@dataclass(frozen=True)
class PinnedDigestHttpTransport:
    """HTTP Digest GET-only transport pinned to a resolved private address."""

    host: str
    address: str
    port: int
    username: str
    password: str
    timeout: float = 5.0
    max_response_bytes: int = 262_144

    def get(self, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        ReadOnlyHttpTransport(f"http://{self.host}:{self.port}", "prusalink")._validate(path)
        if headers:
            raise PassivePolicyError("digest transport does not accept caller headers")
        bracketed = f"[{self.address}]" if ":" in self.address else self.address
        with requests.Session() as session:
            session.trust_env = False
            # PrusaLink Digest is LAN-only HTTP; address pinning and trust_env=False prevent rerouting.
            # nosemgrep: python.lang.security.audit.insecure-transport.requests.request-session-http-in-with-context.request-session-http-in-with-context
            response = session.get(
                f"http://{bracketed}:{self.port}{path}",  # nosemgrep: python.lang.security.audit.insecure-transport.requests.request-with-http.request-with-http, python.lang.security.audit.insecure-transport.requests.request-session-http-in-with-context.request-session-http-in-with-context -- private-LAN Digest; pinned peer; proxies disabled
                headers={"Accept": "application/json", "Accept-Encoding": "identity", "Host": self.host},
                auth=HTTPDigestAuth(self.username, self.password), timeout=self.timeout,
                allow_redirects=False, stream=True,
            )
            try:
                if 300 <= response.status_code < 400:
                    raise PassivePolicyError("redirects are disabled for passive certification")
                if response.status_code < 200 or response.status_code >= 300:
                    raise PassivePolicyError("passive endpoint returned an error status")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"application/json", "text/json"}:
                    raise PassivePolicyError("passive response must be JSON")
                if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    raise PassivePolicyError("compressed passive responses are disabled")
                raw = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    raw.extend(chunk)
                    if len(raw) > self.max_response_bytes:
                        raise PassivePolicyError("passive response exceeds size limit")
                return decode_bounded_json_object(bytes(raw).decode("utf-8"))
            finally:
                response.close()

class PassiveMqttTransport:
    """Connect/subscribe/disconnect only; no public generic client accessor."""

    def __init__(self, client: Any, device_token: str):
        self.__client = client
        self.__topic = f"device/{device_token}/report"

    def connect(self, host: str, port: int, keepalive: int = 30):
        return self.__client.connect(host, port, keepalive=keepalive)

    def subscribe(self, topic: str):
        if topic != self.__topic:
            raise PassivePolicyError("only the configured Bambu report topic is allowed")
        return self.__client.subscribe(topic, qos=0)

    def disconnect(self):
        return self.__client.disconnect()

class PassiveWebSocketTransport:
    """Receive/close only; no command-frame surface."""

    def __init__(self, websocket: Any):
        self.__websocket = websocket

    def receive(self):
        return self.__websocket.recv()

    def close(self):
        return self.__websocket.close()
