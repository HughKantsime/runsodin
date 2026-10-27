"""The port-6000 protocol against a local, self-signed TLS printer double."""
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import ssl
import struct

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from modules.printers import bambu_chamber_camera as chamber

CODE = "synthetic-code"
JPEG = b"\xff\xd8synthetic-frame\xff\xd9"
SOURCE = chamber.ChamberSource("printer.test", CODE)


@pytest.fixture
def fake_printer(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-printer")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    monkeypatch.setattr(chamber, "IDLE_GRACE", 0.05)
    monkeypatch.setattr(chamber, "FRAME_TIMEOUT", 1)
    policy_calls = []

    def policy(host, port):
        policy_calls.append((host, port))
        assert host == "printer.test"
        return "127.0.0.1"
    monkeypatch.setattr(chamber, "device_connect_ip", policy)

    @asynccontextmanager
    async def serve(mode="stream", payload=JPEG):
        auth, closed, tasks = [], [], set()
        async def handle(reader, writer):
            tasks.add(asyncio.current_task())
            try:
                if mode == "handshake":
                    await reader.read()
                    return
                auth.append(await reader.readexactly(80))
                if mode == "stall":
                    await reader.read()
                    return
                count = 0
                while True:
                    frame = payload if mode != "stream" else JPEG + str(count).encode()
                    length = chamber.MAX_FRAME_BYTES + 1 if mode == "oversize" else len(frame)
                    wire = struct.pack("<IIII", length, 0, 0, 0) + frame
                    # Force both the header and payload across separate reads.
                    for part in (wire[:3], wire[3:11], wire[11:18], wire[18:]):
                        writer.write(part)
                        await writer.drain()
                        await asyncio.sleep(0.002)
                    if mode != "stream":
                        await reader.read()
                        return
                    count += 1
                    await asyncio.sleep(0.01)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                closed.append(True)
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
                tasks.discard(asyncio.current_task())
        try:
            server = await asyncio.start_server(handle, "127.0.0.1", 0,
                                                ssl=None if mode == "handshake" else context)
        except PermissionError:
            pytest.skip("Sandbox forbids binding the local TLS fake printer")
        monkeypatch.setattr(chamber, "PORT", server.sockets[0].getsockname()[1])
        try:
            yield auth, closed, policy_calls
        finally:
            server.close()
            await server.wait_closed()
            remaining = list(tasks)
            for task in remaining:
                task.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)
    return serve


def test_auth_and_split_frame_reads(fake_printer):
    async def run():
        async with fake_printer("single") as (auth, closed, calls):
            stream = chamber.frames(SOURCE)
            assert await anext(stream) == JPEG
            assert auth == [struct.pack("<IIII", 0x40, 0x3000, 0, 0)
                            + b"bblp".ljust(32, b"\0") + CODE.encode().ljust(32, b"\0")]
            assert calls == [("printer.test", chamber.PORT)]
            await stream.aclose()
            await asyncio.sleep(0.03)
            assert closed == [True]
    asyncio.run(run())


@pytest.mark.parametrize("mode,payload", [("oversize", JPEG), ("single", b""), ("single", b"not a jpeg")])
def test_invalid_frames_close_without_forwarding(fake_printer, mode, payload):
    async def run():
        async with fake_printer(mode, payload) as (_, closed, _):
            stream = chamber.frames(SOURCE)
            with pytest.raises(chamber.InvalidFrame):
                await anext(stream)
            await asyncio.sleep(0.03)
            assert closed == [True]
    asyncio.run(run())


@pytest.mark.parametrize("mode,timeout", [("stall", "READ_TIMEOUT"), ("handshake", "HANDSHAKE_TIMEOUT")])
def test_read_and_handshake_timeouts(fake_printer, monkeypatch, mode, timeout):
    monkeypatch.setattr(chamber, timeout, 0.04)
    async def run():
        async with fake_printer(mode) as (_, closed, _):
            with pytest.raises((TimeoutError, ConnectionAbortedError)):
                await anext(chamber.frames(SOURCE))
            await asyncio.sleep(0.03)
            assert closed == [True]
    asyncio.run(run())


def test_connect_timeout(fake_printer, monkeypatch):
    monkeypatch.setattr(chamber, "CONNECT_TIMEOUT", 0.02)
    async def run():
        async def stalled(*args):
            await asyncio.sleep(10)
        monkeypatch.setattr(asyncio.get_running_loop(), "sock_connect", stalled)
        with pytest.raises(TimeoutError):
            await anext(chamber.frames(SOURCE))
    asyncio.run(run())


def test_destination_policy_refusal_does_not_connect(monkeypatch):
    from core.webhook_utils import DeviceDestinationBlocked
    def refuse(*args):
        raise DeviceDestinationBlocked("blocked")
    monkeypatch.setattr(chamber, "device_connect_ip", refuse)
    async def run():
        with pytest.raises(DeviceDestinationBlocked):
            await anext(chamber.frames(SOURCE))
    asyncio.run(run())


def test_two_relay_consumers_share_socket_and_disconnect_independently(fake_printer, monkeypatch):
    from modules.printers import camera_relay
    from starlette.requests import Request
    monkeypatch.setattr(camera_relay, "relay_token", lambda pid: "test-token")
    monkeypatch.setattr(camera_relay, "_stored_http_camera_url", lambda pid: None)
    monkeypatch.setattr(camera_relay, "_stored_chamber_camera", lambda pid: SOURCE)
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})
    async def run():
        hub = chamber.ChamberCameraHub()
        monkeypatch.setattr(chamber, "chamber_hub", hub)
        async with fake_printer() as (auth, closed, _):
            responses = [camera_relay.camera_source(7, "test-token", request) for _ in range(2)]
            assert all(r.media_type == "multipart/x-mixed-replace; boundary=odin-chamber" for r in responses)
            a, b = [r.body_iterator for r in responses]
            first = await asyncio.gather(anext(a), anext(b))
            assert first[0] == first[1]
            header, body = first[0].split(b"\r\n\r\n", 1)
            assert b"Content-Type: image/jpeg" in header
            assert f"Content-Length: {len(body) - 2}".encode() in header
            assert body.startswith(b"\xff\xd8") and len(auth) == 1
            await a.aclose()
            # A slow viewer sees the newest frame; there is no per-viewer queue.
            await asyncio.sleep(0.06)
            assert await anext(b) != first[1]
            assert len(auth) == 1 and not closed
            await b.aclose()
            await asyncio.sleep(0.12)
            assert closed == [True] and hub.hubs == {}
    asyncio.run(run())


def test_rejoin_during_grace_reuses_connection(fake_printer):
    async def run():
        hub = chamber.ChamberCameraHub()
        async with fake_printer() as (auth, closed, _):
            stream = hub.multipart(7, SOURCE)
            await anext(stream)
            await stream.aclose()
            stream = hub.multipart(7, SOURCE)
            await anext(stream)
            assert len(auth) == 1
            await stream.aclose()
            await asyncio.sleep(0.12)
            assert closed == [True] and not hub.hubs
    asyncio.run(run())


def test_wait_timeout_releases_consumer(fake_printer, monkeypatch):
    monkeypatch.setattr(chamber, "FRAME_TIMEOUT", 0.04)
    async def run():
        hub = chamber.ChamberCameraHub()
        async with fake_printer("stall") as (_, closed, _):
            with pytest.raises(StopAsyncIteration):
                await anext(hub.multipart(7, SOURCE))
            await asyncio.sleep(0.12)
            assert closed == [True] and not hub.hubs
    asyncio.run(run())


def test_errors_log_only_printer_and_reason_and_backoff_is_bounded(monkeypatch, caplog):
    attempts = []
    async def broken(source):
        attempts.append(True)
        raise ValueError(CODE)
        yield  # async generator double
    monkeypatch.setattr(chamber, "frames", broken)
    async def run():
        hub = chamber._Hub(7, SOURCE)
        await asyncio.sleep(0.02)
        assert attempts == [True]  # reconnect is delayed
        await hub.close()
    asyncio.run(run())
    assert "printer 7: ValueError" in caplog.text and CODE not in caplog.text
    assert CODE not in repr(SOURCE)
    from modules.system.sanitize import sanitize_structure
    assert CODE not in str(sanitize_structure({"logs": caplog.text, "access_code": CODE}))


def test_reconnect_backoff_caps_at_eight_seconds(monkeypatch):
    delays = []
    real_sleep = asyncio.sleep
    async def broken(source):
        raise ConnectionError("synthetic failure")
        yield
    async def sleep(delay):
        delays.append(delay)
        await real_sleep(0)
    monkeypatch.setattr(chamber, "frames", broken)
    monkeypatch.setattr(chamber.asyncio, "sleep", sleep)
    async def run():
        hub = chamber._Hub(7, SOURCE)
        while len(delays) < 6:
            await real_sleep(0)
        await hub.close()
    asyncio.run(run())
    assert delays[:6] == [1, 2, 4, 8, 8, 8]


@pytest.fixture
def in_memory_frames(monkeypatch):
    opened, closed = [], []
    async def stream(source):
        opened.append(source)
        try:
            count = 0
            while True:
                yield JPEG + str(count).encode()
                count += 1
                await asyncio.sleep(0.005)
        finally:
            closed.append(source)
    monkeypatch.setattr(chamber, "frames", stream)
    monkeypatch.setattr(chamber, "IDLE_GRACE", 0.02)
    monkeypatch.setattr(chamber, "FRAME_TIMEOUT", 0.1)
    return opened, closed


def test_latest_frame_fanout_and_grace_without_sockets(in_memory_frames):
    opened, closed = in_memory_frames
    async def run():
        manager = chamber.ChamberCameraHub()
        a, b = manager.multipart(7, SOURCE), manager.multipart(7, SOURCE)
        first = await asyncio.gather(anext(a), anext(b))
        assert first[0] == first[1] and opened == [SOURCE]
        await a.aclose()
        await asyncio.sleep(0.04)
        latest = manager.hubs[7].latest
        assert latest in await anext(b)
        assert not closed
        await b.aclose()
        c = manager.multipart(7, SOURCE)
        await anext(c)
        assert opened == [SOURCE]
        await c.aclose()
        await asyncio.sleep(0.04)
        assert closed == [SOURCE] and not manager.hubs
    asyncio.run(run())


def test_configuration_change_closes_old_stream_before_opening_new(in_memory_frames):
    opened, closed = in_memory_frames
    async def run():
        manager = chamber.ChamberCameraHub()
        a = manager.multipart(7, SOURCE)
        await anext(a)
        updated = chamber.ChamberSource("printer.test", "new-synthetic-code")
        b = manager.multipart(7, updated)
        await anext(b)
        assert opened == [SOURCE, updated] and closed == [SOURCE]
        with pytest.raises(StopAsyncIteration):
            await anext(a)
        await b.aclose()
        await asyncio.sleep(0.04)
        assert closed == [SOURCE, updated] and not manager.hubs
    asyncio.run(run())


def test_asgi_disconnect_during_send_releases_subscription(in_memory_frames):
    opened, closed = in_memory_frames
    async def run():
        manager = chamber.ChamberCameraHub()
        response = chamber.ChamberCameraResponse(manager.multipart(7, SOURCE))
        sent = asyncio.Event()
        async def send(message):
            if message["type"] == "http.response.body":
                sent.set()
                await asyncio.sleep(10)
        async def receive():
            await sent.wait()
            return {"type": "http.disconnect"}
        await response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
        await asyncio.sleep(0.04)
        assert opened == closed == [SOURCE] and not manager.hubs
    asyncio.run(run())


def test_cancelled_frame_wait_releases_subscription(monkeypatch):
    closed = []
    async def stalled(source):
        try:
            await asyncio.sleep(10)
            yield JPEG
        finally:
            closed.append(True)
    monkeypatch.setattr(chamber, "frames", stalled)
    monkeypatch.setattr(chamber, "IDLE_GRACE", 0.02)
    async def run():
        manager = chamber.ChamberCameraHub()
        stream = manager.multipart(7, SOURCE)
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.04)
        assert closed == [True] and not manager.hubs
    asyncio.run(run())


@pytest.mark.parametrize("length,payload,expected", [
    (len(JPEG), JPEG, None), (0, b"", chamber.InvalidFrame),
    (chamber.MAX_FRAME_BYTES + 1, b"", chamber.InvalidFrame),
    (3, b"bad", chamber.InvalidFrame), (10, b"short", asyncio.IncompleteReadError),
])
def test_frame_parser_over_split_reads_without_sockets(monkeypatch, length, payload, expected):
    from types import SimpleNamespace
    monkeypatch.setattr(chamber, "device_connect_ip", lambda host, port: "192.0.2.7")
    writes, sockets, closed = [], [], []
    class Writer:
        transport = SimpleNamespace(abort=lambda: closed.append(True))
        def write(self, data):
            writes.append(data)
        async def drain(self):
            pass
        def close(self):
            pass
    async def run():
        reader = asyncio.StreamReader()
        async def connect(sock, address):
            assert address == ("192.0.2.7", 6000)
            sockets.append(sock)
        async def tls(**kwargs):
            assert kwargs["sock"] is sockets[0]
            assert kwargs["ssl"].verify_mode == ssl.CERT_NONE
            return reader, Writer()
        monkeypatch.setattr(asyncio.get_running_loop(), "sock_connect", connect)
        monkeypatch.setattr(chamber.asyncio, "open_connection", tls)
        async def feed():
            wire = struct.pack("<IIII", length, 0, 0, 0) + payload
            for offset in range(0, len(wire), 3):
                reader.feed_data(wire[offset:offset + 3])
                await asyncio.sleep(0)
            reader.feed_eof()
        feeding = asyncio.create_task(feed())
        stream = chamber.frames(SOURCE)
        if expected:
            with pytest.raises(expected):
                await anext(stream)
        else:
            assert await anext(stream) == JPEG
        await stream.aclose()
        await feeding
        assert writes == [struct.pack("<IIII", 0x40, 0x3000, 0, 0)
                          + b"bblp".ljust(32, b"\0") + CODE.encode().ljust(32, b"\0")]
        assert sockets[0].fileno() == -1 and closed == [True]
    asyncio.run(run())
