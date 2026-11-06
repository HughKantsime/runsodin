"""Outbound saturation and deadlines must not block the shared asyncio pool."""

import asyncio
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import httpx
import pytest

from core import outbound_http, webhook_utils


@pytest.fixture
def isolated_pool(monkeypatch):
    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(outbound_http, "_executor", pool, raising=False)
        slots = asyncio.Semaphore(2)
        monkeypatch.setattr(outbound_http, "_get_slots", lambda: slots)
        yield pool


async def wait_started(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def test_blackholed_requests_do_not_starve_unrelated_to_thread(monkeypatch, isolated_pool):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    release = threading.Event()
    connections = []

    def blackhole(sock, address):
        connections.append(address)
        assert release.wait(5), "test failed to release blackholed connects"
        raise ConnectionRefusedError("synthetic blackhole released")

    monkeypatch.setattr(socket.socket, "connect", blackhole)

    async def run():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        tasks = [asyncio.create_task(outbound_http.pinned_async_request(
            "GET", "http://10.255.255.1", timeout=4,
        )) for _ in range(40)]
        try:
            await wait_started(lambda: len(connections) == 2)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "event-feed"), 0.25) == "event-feed"
            assert not any(task.done() for task in tasks)
            # Cancel queued requests without ever starting their network work.
            for task in tasks[2:]:
                task.cancel()
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert len(connections) == 2, "cancelled waiters must never start a connection"

    asyncio.run(run())


def test_total_deadline_includes_dns_and_prevents_late_request(monkeypatch, isolated_pool):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    release = threading.Event()
    started = threading.Event()
    connected = []

    def resolve(host, port, **kwargs):
        started.set()
        assert release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]

    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: connected.append(True))

    async def run():
        task = asyncio.create_task(outbound_http.pinned_async_request(
            "POST", "http://slow-dns.test", timeout=0.05,
        ))
        try:
            await wait_started(started.is_set)
            with pytest.raises(httpx.TimeoutException):
                await asyncio.wait_for(task, 0.25)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    isolated_pool.shutdown(wait=True)
    assert connected == []


@pytest.mark.parametrize("cancel", [True, False])
def test_cancel_or_deadline_holds_capacity_until_worker_finishes(monkeypatch, isolated_pool, cancel):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    release = threading.Event()
    started = []
    trace = ContextVar("outbound-test-trace", default=None)
    observed = []

    def blackhole(sock, address):
        observed.append(trace.get())
        started.append(True)
        assert release.wait(5)
        raise ConnectionRefusedError("released")

    monkeypatch.setattr(socket.socket, "connect", blackhole)

    async def run():
        trace.set("copied-context")
        tasks = [asyncio.create_task(outbound_http.pinned_async_request(
            "GET", "http://10.0.0.1", timeout=2 if cancel else 0.1,
        )) for _ in range(2)]
        try:
            await wait_started(lambda: len(started) == 2)
            if cancel:
                for task in tasks:
                    task.cancel()
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 0.5)
            expected = asyncio.CancelledError if cancel else httpx.TimeoutException
            assert all(isinstance(result, expected) for result in results)
            queued_at = time.monotonic()
            with pytest.raises(httpx.TimeoutException):
                await outbound_http.pinned_async_request("GET", "http://10.0.0.1", timeout=0.05)
            assert 0.04 <= time.monotonic() - queued_at < 0.5
            assert len(started) == 2, "expired queue wait must not submit a request"
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())
    isolated_pool.shutdown(wait=True)
    assert observed == ["copied-context"] * 2
