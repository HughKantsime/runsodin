"""Burst admission, total streaming deadlines and bounded response buffering."""

import asyncio
import errno
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from core import outbound_http


@pytest.fixture
def capacity(monkeypatch):
    @contextmanager
    def use(workers):
        slots = asyncio.Semaphore(workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            monkeypatch.setattr(outbound_http, "_executor", pool)
            monkeypatch.setattr(outbound_http, "_get_slots", lambda: slots, raising=False)
            yield slots
    return use


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/burst":
                time.sleep(0.3)
            self.send_response(200)
            self.send_header("Content-Length", "100" if self.path == "/dribble" else "2")
            self.end_headers()
            try:
                if self.path == "/dribble":
                    for _ in range(100):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.5)
                else:
                    self.wfile.write(b"ok")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        request_queue_size = 64
        daemon_threads = True

    try:
        server = Server(("127.0.0.1", 0), Handler)
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES):
            pytest.skip(f"blocked: sandbox denies loopback server socket: {exc}")
        raise
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("real_server", [False, True])
def test_thirty_concurrent_300ms_requests_succeed(monkeypatch, request, real_server):
    url = request.getfixturevalue("upstream") + "/burst" if real_server else "http://127.0.0.1"
    if not real_server:
        client = httpx.Client

        def respond(request):
            time.sleep(0.3)
            return httpx.Response(200, content=b"ok")

        monkeypatch.setattr(outbound_http.httpx, "Client", lambda **kw: client(
            transport=httpx.MockTransport(respond), **kw))

    async def run():
        started = time.monotonic()
        results = await asyncio.gather(*[
            outbound_http.pinned_async_request("GET", url, timeout=3) for _ in range(30)
        ], return_exceptions=True)
        assert all(isinstance(result, httpx.Response) for result in results), results
        assert all(result.content == b"ok" for result in results)
        assert time.monotonic() - started < 3

    asyncio.run(run())


@pytest.mark.parametrize("real_server", [False, True])
def test_dribbling_body_releases_slot_at_deadline(monkeypatch, request, capacity, real_server):
    closed = threading.Event()
    url = request.getfixturevalue("upstream") if real_server else "http://127.0.0.1"
    if not real_server:
        class Dribble(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(8):
                    yield b"x"
                    time.sleep(0.5)

            def close(self):
                closed.set()

        def respond(request):
            return (httpx.Response(200, stream=Dribble()) if request.url.path == "/dribble"
                    else httpx.Response(200, content=b"ok"))

        client = httpx.Client
        monkeypatch.setattr(outbound_http.httpx, "Client", lambda **kw: client(
            transport=httpx.MockTransport(respond), **kw))

    async def run():
        started = time.monotonic()
        with pytest.raises(httpx.TimeoutException):
            await outbound_http.pinned_async_request("GET", url + "/dribble", timeout=2)
        response = await outbound_http.pinned_async_request("GET", url + "/ok", timeout=0.8)
        assert response.content == b"ok"
        assert time.monotonic() - started < 2.8
        if not real_server:
            assert closed.is_set()

    with capacity(1):
        asyncio.run(run())


@pytest.mark.parametrize("limit", [None, 1024])
def test_body_size_limit_closes_response_without_buffering_rest(monkeypatch, limit):
    chunks = []
    closed = []
    bound = 4 * 1024 * 1024 if limit is None else limit

    class LargeBody(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                chunks.append(True)
                yield b"x" * (bound // 2)

        def close(self):
            closed.append(True)

    client = httpx.Client
    monkeypatch.setattr(outbound_http.httpx, "Client", lambda **kw: client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=LargeBody())), **kw))
    kwargs = {} if limit is None else {"max_response_bytes": limit}
    with pytest.raises(outbound_http.OutboundResponseTooLarge, match="size limit"):
        asyncio.run(outbound_http.pinned_async_request("GET", "http://127.0.0.1", **kwargs))
    assert len(chunks) == 3
    assert closed == [True]
