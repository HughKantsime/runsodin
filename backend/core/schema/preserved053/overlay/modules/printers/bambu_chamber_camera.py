"""Bambu port-6000 JPEG protocol and process-local, latest-frame fan-out."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import logging
import socket
import ssl
import struct

from fastapi.responses import StreamingResponse

from core.webhook_utils import device_connect_ip

log = logging.getLogger("odin.camera_relay")
PORT = 6000
MAX_FRAME_BYTES = 4 * 1024 * 1024
CONNECT_TIMEOUT = 5
HANDSHAKE_TIMEOUT = 5
READ_TIMEOUT = 15
FRAME_TIMEOUT = 30
IDLE_GRACE = 5
MAX_BACKOFF = 8
SOURCE = "bambu-chamber://auto"
BOUNDARY = "odin-chamber"

# P1S and A1 verified on hardware 2026-10-26; P1P and A1 Mini use the
# same documented protocol (OpenBambuAPI / ha-bambulab).
BAMBU_CHAMBER_CAMERA_MODELS = frozenset({"P1P", "P1S", "A1", "A1 Mini"})


@dataclass(frozen=True)
class ChamberSource:
    host: str = field(repr=False)
    access_code: str = field(repr=False)


def chamber_source(printer):
    """Derived only; a configured camera always takes precedence."""
    from core import crypto
    from modules.printers.printer_models import bambu_model_name
    if (printer.camera_url or printer.api_type != "bambu"
            or bambu_model_name(printer.model) not in BAMBU_CHAMBER_CAMERA_MODELS
            or not printer.api_host or not printer.api_key):
        return None
    try:
        serial, code = crypto.decrypt(printer.api_key).split("|")
        if serial and code and len(code.encode()) <= 32 and "\0" not in code:
            return ChamberSource(printer.api_host, code)
    except Exception:
        pass
    return None


def drop_unchanged_marker(update_data, printer):
    """The auto chamber marker from GET sent back unchanged on update: nothing to store."""
    if update_data.get("camera_url") == SOURCE and chamber_source(printer):
        del update_data["camera_url"]


class InvalidFrame(ValueError):
    pass


async def frames(source):
    """Connect to the policy-approved literal, authenticate, then yield bounded JPEGs."""
    address = await asyncio.wait_for(
        asyncio.to_thread(device_connect_ip, source.host, PORT), CONNECT_TIMEOUT)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # printer certificates are self-signed
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    writer = None
    try:
        await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, (address, PORT)), CONNECT_TIMEOUT)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            sock=sock, ssl=context, server_hostname="", ssl_handshake_timeout=HANDSHAKE_TIMEOUT),
            HANDSHAKE_TIMEOUT)
        writer.write(struct.pack("<IIII", 0x40, 0x3000, 0, 0)
                     + b"bblp".ljust(32, b"\0") + source.access_code.encode().ljust(32, b"\0"))
        await asyncio.wait_for(writer.drain(), READ_TIMEOUT)
        while True:
            # One deadline covers header and payload, including a peer that trickles bytes.
            async with asyncio.timeout(READ_TIMEOUT):
                header = await reader.readexactly(16)
                length = struct.unpack_from("<I", header)[0]
                if not 0 < length <= MAX_FRAME_BYTES:
                    raise InvalidFrame("invalid_length")
                payload = await reader.readexactly(length)
                if not payload.startswith(b"\xff\xd8"):
                    raise InvalidFrame("invalid_jpeg")
            yield payload
    finally:
        if writer is not None:
            writer.close()
            # Do not let a silent TLS peer hold the printer's connection slot open.
            writer.transport.abort()
        sock.close()


class _Hub:
    def __init__(self, printer_id, source):
        self.printer_id = printer_id
        self.source = source
        self.condition = asyncio.Condition()
        self.latest = None
        self.sequence = 0
        self.users = 0
        self.stopped = False
        self.expiry = None
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        backoff = 1
        try:
            while True:
                stream = frames(self.source)
                try:
                    async for frame in stream:
                        async with self.condition:
                            self.latest = frame
                            self.sequence += 1
                            self.condition.notify_all()
                        backoff = 1
                except Exception as exc:
                    log.warning("Chamber camera printer %s: %s", self.printer_id, type(exc).__name__)
                finally:
                    await stream.aclose()
                    async with self.condition:
                        self.latest = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
        finally:
            async with self.condition:
                self.stopped = True
                self.condition.notify_all()

    async def next_frame(self, sequence):
        async with asyncio.timeout(FRAME_TIMEOUT):
            async with self.condition:
                await self.condition.wait_for(lambda: self.stopped or (
                    self.latest is not None and self.sequence != sequence))
                if self.stopped:
                    raise EOFError
                return self.sequence, self.latest

    async def close(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task


class ChamberCameraHub:
    """One hub per printer; replacement and expiry wait for the old socket to close."""
    def __init__(self):
        self.hubs = {}
        self.lock = asyncio.Lock()

    async def acquire(self, printer_id, source):
        async with self.lock:
            hub = self.hubs.get(printer_id)
            if hub and hub.expiry:
                hub.expiry.cancel()
                hub.expiry = None
            if hub and hub.source != source:
                await hub.close()
                hub = None
            if hub is None:
                hub = self.hubs[printer_id] = _Hub(printer_id, source)
            hub.users += 1
            return hub

    async def release(self, hub):
        async with self.lock:
            hub.users -= 1
            if hub.users == 0 and self.hubs.get(hub.printer_id) is hub:
                hub.expiry = asyncio.create_task(self._expire(hub))

    async def _expire(self, hub):
        await asyncio.sleep(IDLE_GRACE)
        async with self.lock:
            if hub.users == 0 and self.hubs.get(hub.printer_id) is hub:
                await hub.close()
                del self.hubs[hub.printer_id]

    async def multipart(self, printer_id, source):
        hub = await self.acquire(printer_id, source)
        sequence = 0
        try:
            while True:
                sequence, frame = await hub.next_frame(sequence)
                yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                       f"Content-Length: {len(frame)}\r\n\r\n").encode() + frame + b"\r\n"
        except (TimeoutError, EOFError):
            return
        finally:
            await asyncio.shield(self.release(hub))


chamber_hub = ChamberCameraHub()


class ChamberCameraResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A disconnect during ASGI send can leave the generator suspended at yield.
            await asyncio.shield(self.body_iterator.aclose())
