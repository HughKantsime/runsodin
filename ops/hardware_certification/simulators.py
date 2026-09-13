"""Loopback-only protocol peers for deterministic certification replay."""

from __future__ import annotations

import json
import http.server
import socket
import shutil
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from websockets.sync.server import serve
from websockets.exceptions import ConnectionClosed


def _remaining_length(value: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = value % 128
        value //= 128
        if value:
            digit |= 0x80
        encoded.append(digit)
        if not value:
            return bytes(encoded)


def _packet(kind: int, payload: bytes) -> bytes:
    return bytes([kind]) + _remaining_length(len(payload)) + payload


def _publish(topic: str, payload: dict[str, Any]) -> bytes:
    topic_bytes = topic.encode("utf-8")
    body = len(topic_bytes).to_bytes(2, "big") + topic_bytes + json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _packet(0x30, body)


def _read_exact(stream, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = stream.read(length - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def _read_mqtt_packet(stream) -> tuple[int, bytes]:
    first = stream.read(1)
    if not first:
        raise EOFError
    multiplier = 1
    remaining = 0
    for _ in range(4):
        digit = _read_exact(stream, 1)[0]
        remaining += (digit & 127) * multiplier
        if not digit & 128:
            break
        multiplier *= 128
    else:
        raise ValueError("invalid MQTT remaining length")
    return first[0], _read_exact(stream, remaining)


class MiniTlsMqttBroker:
    """Minimal TLS MQTT 3.1.1 peer: CONNECT, SUBSCRIBE, two reports, close."""

    def __init__(
        self, device_token: str, reports: list[dict[str, Any]],
        access_code: str = "ODIN-CERT-FICTIONAL-ACCESS",
        *, session_reports: list[list[dict[str, Any]]] | None = None,
        active_remote_name: str | None = None,
        command_reports: dict[str, list[dict[str, Any]]] | None = None,
        command_sequence: list[str] | None = None,
    ):
        self.device_token = device_token
        self.reports = reports
        self.access_code = access_code
        self.session_reports = session_reports
        self.active_remote_name = active_remote_name
        self.command_reports = command_reports or {}
        self.command_sequence = command_sequence or []
        self.authenticated = False
        self.subscriptions: list[str] = []
        self.published_messages: list[tuple[str, dict[str, Any]]] = []
        self.unexpected_packet_types: list[int] = []
        self.command_errors: list[str] = []
        self.connection_count = 0
        self._connection_lock = threading.Lock()
        self._subscribers: list[Any] = []
        self._subscriber_lock = threading.Lock()
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._handler_threads: list[threading.Thread] = []

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("broker is not started")
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("broker is already started")
        openssl = shutil.which("openssl")
        if openssl is None:
            raise RuntimeError("openssl is required for TLS replay")
        self._temporary = tempfile.TemporaryDirectory(prefix="odin-cert-mqtt-")
        root = Path(self._temporary.name)
        key = root / "key.pem"
        certificate = root / "certificate.pem"
        subprocess.run(
            [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=ODIN-CERT-FICTIONAL", "-keyout", str(key), "-out", str(certificate)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        owner = self

        def broadcast(reports: list[dict[str, Any]]) -> None:
            with owner._subscriber_lock:
                subscribers = list(owner._subscribers)
            for stream in subscribers:
                try:
                    for report in reports:
                        stream.write(_publish(f"device/{owner.device_token}/report", report))
                    stream.flush()
                except (OSError, ValueError):
                    with owner._subscriber_lock:
                        if stream in owner._subscribers:
                            owner._subscribers.remove(stream)

        class Handler(socketserver.StreamRequestHandler):
            def setup(self):
                owner._handler_threads.append(threading.current_thread())
                super().setup()

            def handle(self):
                with owner._connection_lock:
                    session_index = owner.connection_count
                    owner.connection_count += 1
                reports = owner.reports
                if owner.session_reports is not None:
                    reports = owner.session_reports[min(session_index, len(owner.session_reports) - 1)]
                while True:
                    try:
                        first, payload = _read_mqtt_packet(self.rfile)
                    except (EOFError, OSError):
                        return
                    packet_type = first >> 4
                    if packet_type == 1:
                        owner.authenticated = b"bblp" in payload and owner.access_code.encode("utf-8") in payload
                        if not owner.authenticated:
                            self.wfile.write(b"\x20\x02\x00\x04")
                            self.wfile.flush()
                            return
                        self.wfile.write(b"\x20\x02\x00\x00")
                        self.wfile.flush()
                    elif packet_type == 8:
                        packet_id = payload[:2]
                        topic_length = int.from_bytes(payload[2:4], "big")
                        topic = payload[4:4 + topic_length].decode("utf-8")
                        owner.subscriptions.append(topic)
                        self.wfile.write(_packet(0x90, packet_id + b"\x00"))
                        with owner._subscriber_lock:
                            owner._subscribers.append(self.wfile)
                        for report in reports:
                            self.wfile.write(_publish(topic, report))
                        self.wfile.flush()
                        if len(reports) < 2:
                            return
                    elif packet_type == 12:
                        self.wfile.write(b"\xd0\x00")
                        self.wfile.flush()
                    elif packet_type == 3:
                        topic_length = int.from_bytes(payload[:2], "big")
                        topic = payload[2:2 + topic_length].decode("utf-8")
                        try:
                            body = json.loads(payload[2 + topic_length:].decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            owner.unexpected_packet_types.append(packet_type)
                            return
                        if not isinstance(body, dict):
                            owner.unexpected_packet_types.append(packet_type)
                            return
                        owner.published_messages.append((topic, body))
                        print_section = body.get("print")
                        command = print_section.get("command") if isinstance(print_section, dict) else None
                        expected_index = len(owner.published_messages) - 1
                        expected_command = (
                            owner.command_sequence[expected_index]
                            if expected_index < len(owner.command_sequence) else None
                        )
                        valid = topic == f"device/{owner.device_token}/request"
                        valid = valid and command == expected_command
                        if command == "project_file":
                            valid = valid and isinstance(print_section, dict)
                            valid = valid and print_section.get("url") == f"ftp:///{owner.active_remote_name}"
                            valid = valid and print_section.get("subtask_name") == str(owner.active_remote_name).removesuffix(".3mf")
                            valid = valid and print_section.get("param") == "Metadata/plate_1.gcode"
                            valid = valid and isinstance(print_section.get("sequence_id"), str)
                        elif command in {"pause", "resume", "stop"}:
                            valid = valid and print_section == {
                                "sequence_id": "0", "command": command,
                            }
                        if not valid:
                            owner.command_errors.append("unexpected MQTT command payload")
                            continue
                        broadcast(owner.command_reports.get(str(command), []))
                    elif packet_type == 14:
                        return
                    else:
                        owner.unexpected_packet_types.append(packet_type)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = False
            daemon_threads = True

        server = Server((self.host, 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("MQTT replay server thread did not terminate")
        for handler_thread in self._handler_threads:
            handler_thread.join(timeout=2)
            if handler_thread.is_alive():
                raise RuntimeError("MQTT replay handler thread did not terminate")
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_args):
        self.stop()


@dataclass(frozen=True)
class HttpReply:
    status: int = 200
    body: bytes = b"{}"
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)
    delay_seconds: float = 0.0
    disconnect: bool = False


class MiniJsonHttpPeer:
    """Loopback JSON peer with a complete in-memory request transcript."""

    def __init__(
        self,
        responses: dict[object, dict[str, Any] | HttpReply | list[dict[str, Any] | HttpReply]],
        *,
        request_handler: Callable[[str, str, bytes, dict[str, str]], dict[str, Any] | HttpReply | None] | None = None,
    ):
        self.responses = responses
        self.request_handler = request_handler
        self.requests: list[tuple[str, str]] = []
        self.request_bodies: list[bytes] = []
        self.auth_headers: list[tuple[str, bool, bool]] = []
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._handler_threads: list[threading.Thread] = []

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("HTTP peer is not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def setup(self):
                owner._handler_threads.append(threading.current_thread())
                super().setup()

            def _respond(self):
                owner.requests.append((self.command, self.path))
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                owner.request_bodies.append(body)
                owner.auth_headers.append((
                    self.path, bool(self.headers.get("X-Api-Key")),
                    bool(self.headers.get("Authorization")),
                ))
                configured = (
                    owner.request_handler(
                        self.command, self.path, body,
                        {name: value for name, value in self.headers.items()},
                    )
                    if owner.request_handler is not None else None
                )
                if configured is None:
                    configured = owner.responses.get((self.command, self.path))
                if configured is None and self.command == "GET":
                    configured = owner.responses.get(self.path)
                if configured is None:
                    self.send_response(405 if self.command != "GET" else 404)
                    self.end_headers()
                    return
                if isinstance(configured, list):
                    if not configured:
                        self.send_response(503)
                        self.end_headers()
                        return
                    configured = configured.pop(0)
                reply = configured if isinstance(configured, HttpReply) else HttpReply(
                    body=json.dumps(configured, separators=(",", ":")).encode("utf-8")
                )
                if reply.delay_seconds:
                    time.sleep(reply.delay_seconds)
                if reply.disconnect:
                    self.close_connection = True
                    return
                body = reply.body
                self.send_response(reply.status)
                self.send_header("Content-Type", reply.content_type)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    try:
                        self.wfile.write(body)
                    except BrokenPipeError:
                        pass

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _respond

            def log_message(self, *_args):
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        failures: list[str] = []
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                failures.append("HTTP replay server thread")
        for handler_thread in self._handler_threads:
            handler_thread.join(timeout=2)
            if handler_thread.is_alive():
                failures.append("HTTP replay handler thread")
        if failures:
            raise RuntimeError(f"{', '.join(failures)} did not terminate")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_args):
        self.stop()


class StatefulHardwareHttpPeer:
    """Causal Moonraker/PrusaLink peer: commands mutate state before telemetry changes."""

    def __init__(
        self, protocol: str, remote_name: str, expected_asset: bytes,
        *, job_id: int = 71,
    ):
        if protocol not in {"moonraker", "prusalink"}:
            raise ValueError("stateful HTTP peer protocol is invalid")
        self.protocol = protocol
        self.remote_name = remote_name
        self.expected_asset = expected_asset
        self.job_id = job_id
        self.state = "idle"
        self.uploaded = False
        self.sequence = 0
        self.command_errors: list[str] = []
        self.peer = MiniJsonHttpPeer({}, request_handler=self._handle)

    @property
    def base_url(self) -> str:
        return self.peer.base_url

    @property
    def requests(self) -> list[tuple[str, str]]:
        return self.peer.requests

    def _multipart_valid(self, body: bytes, fields: dict[str, str]) -> bool:
        required = [
            f'filename="{self.remote_name}"'.encode(),
            self.expected_asset,
        ]
        required.extend(
            f'name="{name}"\r\n\r\n{value}'.encode()
            for name, value in fields.items()
        )
        return all(value in body for value in required)

    def _moon_status(self) -> dict[str, Any]:
        state = {
            "idle": "standby", "printing": "printing", "paused": "paused",
            "stopped": "cancelled",
        }[self.state]
        return {"result": {"eventtime": float(self.sequence), "status": {
            "print_stats": {
                "state": state,
                "filename": self.remote_name if self.uploaded else "",
                "print_duration": float(self.sequence),
            },
        }}}

    def _prusa_status(self) -> dict[str, Any]:
        state = {
            "idle": "IDLE", "printing": "PRINTING", "paused": "PAUSED",
            "stopped": "STOPPED",
        }[self.state]
        return {
            "telemetry_sequence": self.sequence,
            "printer": {"state": state},
        }

    def _prusa_job(self) -> dict[str, Any]:
        return {"job": {
            "id": self.job_id if self.uploaded else None,
            "time_printing": self.sequence,
            "file": {"name": self.remote_name if self.uploaded else ""},
        }}

    def _reject(self, reason: str) -> HttpReply:
        self.command_errors.append(reason)
        return HttpReply(status=409)

    def _handle(
        self, method: str, path: str, body: bytes, _headers: dict[str, str],
    ) -> dict[str, Any] | HttpReply | None:
        if method == "GET":
            self.sequence += 1
            if self.protocol == "moonraker" and path == MOON_STATUS_PATH:
                return self._moon_status()
            if self.protocol == "prusalink" and path == "/api/v1/status":
                return self._prusa_status()
            if self.protocol == "prusalink" and path == "/api/job":
                return self._prusa_job()
            return None
        if self.protocol == "moonraker":
            if method == "POST" and path == "/server/files/upload":
                if self.state != "idle" or not self._multipart_valid(body, {"root": "gcodes"}):
                    return self._reject("invalid Moonraker upload")
                self.uploaded = True
                return HttpReply(status=201)
            if method == "POST" and path == f"/printer/print/start?filename={self.remote_name}":
                if self.state != "idle" or not self.uploaded:
                    return self._reject("invalid Moonraker start")
                self.state = "printing"
                return HttpReply(status=200)
            expected = {
                "/printer/print/pause": ("printing", "paused"),
                "/printer/print/resume": ("paused", "printing"),
                "/printer/print/cancel": ("printing", "stopped"),
            }.get(path)
            if method != "POST" or expected is None or self.state != expected[0]:
                return self._reject("invalid Moonraker command")
            self.state = expected[1]
            return HttpReply(status=200)
        if method == "POST" and path == "/api/files/local":
            if self.state != "idle" or not self._multipart_valid(
                body, {"select": "true", "print": "true"},
            ):
                return self._reject("invalid PrusaLink upload-start")
            self.uploaded = True
            self.state = "printing"
            return HttpReply(status=201)
        expected = {
            ("PUT", f"/api/v1/job/{self.job_id}/pause"): ("printing", "paused"),
            ("PUT", f"/api/v1/job/{self.job_id}/resume"): ("paused", "printing"),
            ("DELETE", f"/api/v1/job/{self.job_id}"): ("printing", "stopped"),
        }.get((method, path))
        if expected is None or self.state != expected[0]:
            return self._reject("invalid PrusaLink command")
        self.state = expected[1]
        return HttpReply(status=200)

    def __enter__(self):
        self.peer.start()
        return self

    def __exit__(self, *_args):
        self.peer.stop()


MOON_STATUS_PATH = (
    "/printer/objects/query?extruder&fan&gcode_move&heater_bed&"
    "print_stats&virtual_sdcard&webhooks"
)


class MiniWebSocketPeer:
    """Loopback peer that emits only unsolicited fictional SDCP frames."""

    def __init__(
        self, frames: list[str], *, session_frames: list[list[str]] | None = None,
        max_client_frames: int = 1,
    ):
        self.frames = frames
        self.session_frames = session_frames
        self.client_frames: list[str | bytes] = []
        self.connection_count = 0
        self._connection_lock = threading.Lock()
        self.max_client_frames = max_client_frames
        self._server = None
        self._thread: threading.Thread | None = None
        self._handler_threads: list[threading.Thread] = []

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("WebSocket peer is not started")
        return f"ws://127.0.0.1:{self._server.socket.getsockname()[1]}"

    def start(self) -> None:
        owner = self

        def handler(websocket):
            owner._handler_threads.append(threading.current_thread())
            with owner._connection_lock:
                session_index = owner.connection_count
                owner.connection_count += 1
            frames = owner.frames
            if owner.session_frames is not None:
                frames = owner.session_frames[min(session_index, len(owner.session_frames) - 1)]
            for frame in frames:
                websocket.send(frame)
            if len(frames) < 2:
                return
            for _ in range(owner.max_client_frames):
                try:
                    owner.client_frames.append(websocket.recv(timeout=1.0))
                except (TimeoutError, EOFError, ConnectionClosed):
                    break

        self._server = serve(handler, "127.0.0.1", 0, close_timeout=0.25)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("WebSocket replay server thread did not terminate")
        for handler_thread in self._handler_threads:
            handler_thread.join(timeout=2)
            if handler_thread.is_alive():
                raise RuntimeError("WebSocket replay handler thread did not terminate")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_args):
        self.stop()


class StatefulElegooPeer:
    """Causal SDCP peer that emits state only after an exact command frame."""

    def __init__(self, mainboard_id: str, filename: str):
        self.mainboard_id = mainboard_id
        self.filename = filename
        self.client_frames: list[str | bytes] = []
        self.command_errors: list[str] = []
        self._server = None
        self._thread: threading.Thread | None = None
        self._handler_threads: list[threading.Thread] = []
        self._tick = 0

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("WebSocket peer is not started")
        return f"ws://127.0.0.1:{self._server.socket.getsockname()[1]}"

    def _frame(self, state: str) -> str:
        self._tick += 1
        current, print_state = {
            "printing": (1, 8), "paused": (0, 5), "stopped": (0, 0),
        }[state]
        return json.dumps({
            "Topic": f"sdcp/status/{self.mainboard_id}",
            "Status": {
                "CurrentStatus": [current],
                "PrintInfo": {
                    "Status": print_state, "Filename": self.filename,
                    "CurrentTicks": self._tick,
                },
            },
        }, separators=(",", ":"))

    def start(self) -> None:
        owner = self

        def handler(websocket):
            owner._handler_threads.append(threading.current_thread())
            state = "printing"
            for _ in range(2):
                websocket.send(owner._frame(state))
            for expected_code, next_state in ((129, "paused"), (131, "printing"), (130, "stopped")):
                try:
                    raw = websocket.recv(timeout=3.0)
                    owner.client_frames.append(raw)
                    payload = json.loads(raw)
                except (TimeoutError, EOFError, ConnectionClosed, ValueError, TypeError):
                    owner.command_errors.append("missing or malformed SDCP command")
                    return
                data = payload.get("Data") if isinstance(payload, dict) else None
                valid = (
                    isinstance(data, dict)
                    and payload.get("Topic") == f"sdcp/request/{owner.mainboard_id}"
                    and data.get("Cmd") == expected_code
                    and data.get("MainboardID") == owner.mainboard_id
                    and data.get("Data") == {}
                    and data.get("From") == 0
                )
                if not valid:
                    owner.command_errors.append("unexpected SDCP command payload")
                    return
                state = next_state
                for _ in range(2):
                    websocket.send(owner._frame(state))

        self._server = serve(handler, "127.0.0.1", 0, close_timeout=0.25)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("active Elegoo server thread did not terminate")
        for handler_thread in self._handler_threads:
            handler_thread.join(timeout=2)
            if handler_thread.is_alive():
                raise RuntimeError("active Elegoo handler thread did not terminate")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_args):
        self.stop()


class MiniImplicitFtpsServer:
    """Minimal implicit-TLS FTPS peer validating one protected STOR transfer."""

    def __init__(
        self, remote_name: str, expected_data: bytes,
        access_code: str = "ODIN-CERT-FICTIONAL-ACCESS",
    ):
        self.remote_name = remote_name
        self.expected_data = expected_data
        self.access_code = access_code
        self.received_name: str | None = None
        self.received_data: bytes | None = None
        self.command_errors: list[str] = []
        self.commands: list[str] = []
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._handler_threads: list[threading.Thread] = []

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("FTPS peer is not started")
        return int(self._server.server_address[1])

    def start(self) -> None:
        openssl = shutil.which("openssl")
        if openssl is None:
            raise RuntimeError("openssl is required for TLS replay")
        self._temporary = tempfile.TemporaryDirectory(prefix="odin-cert-ftps-")
        root = Path(self._temporary.name)
        key = root / "key.pem"
        certificate = root / "certificate.pem"
        subprocess.run(
            [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=printer.cert.test", "-keyout", str(key), "-out", str(certificate)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def setup(self):
                owner._handler_threads.append(threading.current_thread())
                super().setup()

            def handle(self):
                passive: socket.socket | None = None
                self.wfile.write(b"220 ODIN certification FTPS\r\n")
                self.wfile.flush()
                try:
                    while True:
                        line = self.rfile.readline(8192)
                        if not line:
                            return
                        command = line.decode("utf-8", errors="strict").strip()
                        owner.commands.append(command)
                        verb, _, argument = command.partition(" ")
                        if verb == "USER" and argument == "bblp":
                            response = b"331 Password required\r\n"
                        elif verb == "PASS" and argument == owner.access_code:
                            response = b"230 Logged in\r\n"
                        elif verb == "PBSZ" and argument == "0":
                            response = b"200 PBSZ accepted\r\n"
                        elif verb == "PROT" and argument == "P":
                            response = b"200 Protection private\r\n"
                        elif verb == "TYPE" and argument == "I":
                            response = b"200 Binary mode\r\n"
                        elif verb == "PASV":
                            if passive is not None:
                                passive.close()
                            passive = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            passive.bind((owner.host, 0))
                            passive.listen(1)
                            passive.settimeout(3.0)
                            port = passive.getsockname()[1]
                            response = (
                                f"227 Entering Passive Mode (127,0,0,1,{port // 256},{port % 256})\r\n"
                            ).encode()
                        elif verb == "STOR":
                            if argument != owner.remote_name or passive is None:
                                owner.command_errors.append("unexpected FTPS STOR target")
                                response = b"550 Invalid target\r\n"
                            else:
                                self.wfile.write(b"150 Opening protected data connection\r\n")
                                self.wfile.flush()
                                raw_data, _peer = passive.accept()
                                protected = context.wrap_socket(raw_data, server_side=True)
                                try:
                                    chunks = []
                                    while chunk := protected.recv(64 * 1024):
                                        chunks.append(chunk)
                                finally:
                                    try:
                                        raw_data = protected.unwrap()
                                    except (OSError, ssl.SSLError):
                                        protected.close()
                                    else:
                                        raw_data.close()
                                passive.close()
                                passive = None
                                owner.received_name = argument
                                owner.received_data = b"".join(chunks)
                                if owner.received_data != owner.expected_data:
                                    owner.command_errors.append("FTPS asset bytes changed")
                                    response = b"550 Invalid data\r\n"
                                else:
                                    response = b"226 Transfer complete\r\n"
                        elif verb == "QUIT":
                            self.wfile.write(b"221 Goodbye\r\n")
                            self.wfile.flush()
                            return
                        else:
                            owner.command_errors.append("unexpected FTPS command")
                            response = b"502 Unsupported command\r\n"
                        self.wfile.write(response)
                        self.wfile.flush()
                finally:
                    if passive is not None:
                        passive.close()

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = False
            daemon_threads = True

        server = Server((self.host, 0), Handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        failures: list[str] = []
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                failures.append("FTPS replay server thread")
        for handler_thread in self._handler_threads:
            handler_thread.join(timeout=2)
            if handler_thread.is_alive():
                failures.append("FTPS replay handler thread")
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        if failures:
            raise RuntimeError(f"{', '.join(failures)} did not terminate")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_args):
        self.stop()
