"""Exercise real httpx resolution; only DNS answers and socket connects are faked."""

import asyncio
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from core import itar, outbound_http, webhook_utils
from modules.organizations.classroom_service import ClassroomError
from modules.organizations.oidc_handler import OIDCHandler


@pytest.mark.parametrize("caller", [
    "discovery", "exchange", "userinfo", "jwks", "classroom", "license_challenge",
])
def test_async_callers_connect_only_to_validated_address(monkeypatch, caller):
    """2.0.44 loses the pin in AsyncClient's DNS worker and connects to loopback.

    Capture the actual socket connect address without opening a network connection.
    The resolver returns a private validated address first, then a rebound address.
    A ContextVar-only test cannot detect this transport integration bug.
    """
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    resolutions = []
    connections = []

    def rebind(host, port, *args, **kwargs):
        resolutions.append(host)
        ip = "10.255.255.1" if len(resolutions) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    def connect(sock, address):
        connections.append(address)
        raise ConnectionRefusedError("regression test stops before sending traffic")

    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", rebind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    url = "http://rebinding.test:8765/identity"
    handler = OIDCHandler("client", "secret", "tenant", "https://callback", discovery_url=url)

    async def config():
        return {"token_endpoint": url, "userinfo_endpoint": url, "jwks_uri": url, "issuer": url}

    if caller != "discovery":
        monkeypatch.setattr(handler, "_get_oidc_config", config)

    async def run():
        from modules.organizations.classroom_service import _json_request

        if caller == "discovery":
            await handler._get_oidc_config()
        elif caller == "exchange":
            await handler.exchange_code("code")
        elif caller == "userinfo":
            await handler.get_user_info("token")
        elif caller == "jwks":
            await handler.parse_id_token("unused-token")
        elif caller == "classroom":
            await _json_request("GET", url)
        else:
            from modules.system.routes_health import _fetch_license_challenge
            await _fetch_license_challenge(url, "test-installation")

    expected = ClassroomError if caller == "classroom" else (httpx.ConnectError, ValueError, HTTPException)
    with pytest.raises(expected) as caught:
        asyncio.run(run())
    if caller == "classroom":
        assert caught.value.status == 502
        assert caught.value.code == "google_request_failed"
    if caller == "license_challenge":
        assert caught.value.status_code == 502
    assert connections == [("10.255.255.1", 8765)]
    assert len(resolutions) == 1, "connecting must not resolve the hostname again"
    assert webhook_utils._dns_pin_state.get() is None


@pytest.mark.parametrize("addresses", [["8.8.8.8"], ["10.0.0.1", "8.8.8.8"], []])
def test_worker_rejects_disallowed_dns_before_connect(monkeypatch, addresses):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
        for ip in addresses
    ])
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("must not connect"))
    with pytest.raises(itar.ItarOutboundBlocked):
        asyncio.run(outbound_http.pinned_async_request("GET", "http://denied.test"))


def test_concurrent_requests_keep_separate_pins(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    barrier = threading.Barrier(2, timeout=5)
    validated = {}
    connections = []

    def resolve(host, port, **kwargs):
        tid = threading.get_ident()
        assert tid not in validated, "connection performed a second DNS lookup"
        ip = "10.0.0.1" if not validated else "10.0.0.2"
        validated[tid] = ip
        barrier.wait()
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    def connect(sock, address):
        barrier.wait()
        assert address[0] == validated[threading.get_ident()]
        connections.append(address[0])
        raise ConnectionRefusedError("no network")

    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", connect)

    async def run():
        return await asyncio.gather(*[
            outbound_http.pinned_async_request("GET", "http://same-host.test") for _ in range(2)
        ], return_exceptions=True)

    results = asyncio.run(run())
    assert all(isinstance(result, httpx.ConnectError) for result in results)
    assert sorted(connections) == ["10.0.0.1", "10.0.0.2"]


@pytest.mark.parametrize("itar_on", [True, False])
def test_request_preserves_host_body_timeout_and_proxy_policy(monkeypatch, itar_on):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1" if itar_on else "0")
    main_thread = threading.get_ident()
    seen = []
    options = {}

    def resolve(host, port, **kwargs):
        assert threading.get_ident() != main_thread
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", port))]

    def upstream(request):
        assert threading.get_ident() != main_thread
        assert bool(webhook_utils._dns_pin_state.get()) == itar_on
        seen.append(request)
        # A redirect must be returned, never followed, even outside ITAR.
        return httpx.Response(302, headers={"location": "http://other.test"}, content=b"buffered")

    def client(**kwargs):
        options.update(kwargs)
        return httpx.Client(transport=httpx.MockTransport(upstream), **kwargs)

    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", resolve)
    monkeypatch.setattr(outbound_http, "httpx", SimpleNamespace(Client=client))
    response = asyncio.run(outbound_http.pinned_async_request(
        "POST", "https://identity.test:8443/token", timeout=15,
        headers={"Authorization": "Bearer test-only"}, data={"code": "test-code"},
    ))
    assert options["trust_env"] is not itar_on
    assert 14 < options["timeout"] <= 15
    assert len(seen) == 1
    assert str(seen[0].url) == "https://identity.test:8443/token"
    assert seen[0].headers["host"] == "identity.test:8443"
    assert seen[0].headers["authorization"] == "Bearer test-only"
    assert seen[0].content == b"code=test-code"
    assert all(14 < value <= options["timeout"] for value in seen[0].extensions["timeout"].values())
    assert response.content == b"buffered"
    assert response.is_closed


@pytest.mark.parametrize("cancel", [True, False])
def test_worker_closes_client_and_resets_pin_after_error_or_cancellation(monkeypatch, cancel):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    started = threading.Event()
    finish = threading.Event()
    closed = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def stream(self, *args, **kwargs):
            assert webhook_utils._dns_pin_state.get()["ips"] == ["10.0.0.1"]
            started.set()
            assert finish.wait(5), "event loop did not release worker"
            raise httpx.ReadTimeout("test timeout")

    monkeypatch.setattr(outbound_http, "httpx", SimpleNamespace(Client=Client, TimeoutException=httpx.TimeoutException))

    async def run():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(outbound_http.pinned_async_request("GET", "http://10.0.0.1"))
        try:
            async with asyncio.timeout(3):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            if cancel:
                task.cancel()
            finish.set()
            with pytest.raises(asyncio.CancelledError if cancel else httpx.ReadTimeout):
                await task
            # Reuse the same worker without copying the event-loop context:
            # this observes its state after request cleanup, including cancellation.
            assert await loop.run_in_executor(pool, webhook_utils._dns_pin_state.get) is None
        finally:
            finish.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(outbound_http, "_executor", pool)
        slots = asyncio.Semaphore(1)
        monkeypatch.setattr(outbound_http, "_get_slots", lambda: slots)
        asyncio.run(run())
    assert closed == [True]
