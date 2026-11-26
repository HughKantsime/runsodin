"""Outbound HTTP with the existing ITAR destination and proxy policy."""

import asyncio
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from contextlib import ExitStack, contextmanager

import httpx

from core import itar, webhook_utils


_WORKERS = 16
_executor = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="odin-outbound")
_DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class OutboundResponseTooLarge(httpx.HTTPError):
    """The decoded response exceeded the limit; retain status without its body."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class OutboundRequestCancelled(Exception):
    """The caller's policy check refused to send the request."""


def _get_slots():
    # Semaphores belong to one event loop. Keep one on that loop so subsequent
    # asyncio.run/TestClient loops cannot reuse a semaphore bound to a dead loop.
    loop = asyncio.get_running_loop()
    if not hasattr(loop, "_odin_outbound_slots"):
        loop._odin_outbound_slots = asyncio.Semaphore(_WORKERS)
    return loop._odin_outbound_slots


def _release_slot(loop, slots):
    # Worker completion may arrive after a cancelled caller's loop has closed.
    try:
        loop.call_soon_threadsafe(slots.release)
    except RuntimeError:
        if not loop.is_closed():
            raise


def _prepare_request(url, timeout, max_response_bytes, open_stream, *, public_only=False, transport=None, cancelled=None):
    """Build the synchronous worker with one deadline for admission, setup and streaming."""
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("outbound timeout must be a positive finite number")
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
        raise ValueError("outbound response size limit must be a positive integer")
    deadline = time.monotonic() + timeout

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0 or (cancelled is not None and cancelled.is_set()):
            raise httpx.TimeoutException("Outbound request total deadline exceeded")
        return budget

    def request():
        remaining()
        with ExitStack() as pins:
            pins.enter_context(itar.pin_for_request(url))
            if public_only:
                ips, port, host, _ = webhook_utils._resolve_and_pin(url)
                pins.enter_context(webhook_utils._pin_dns(host, port, ips))
            options = {"transport": transport} if transport is not None else {}
            with httpx.Client(trust_env=not public_only and itar.should_trust_env(), timeout=remaining(), **options) as client:
                with open_stream(client, remaining()) as response:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        remaining()
                        if len(body) + len(chunk) > max_response_bytes:
                            raise OutboundResponseTooLarge("Outbound response body exceeds size limit",
                                                           status_code=response.status_code)
                        body.extend(chunk)
                    remaining()
                    # Match Response.read(): preserve request, headers, encoding,
                    # extensions and decoded content without decoding it twice.
                    response._content = bytes(body)
                    return response

    return request, remaining


async def _run_in_slot(request, remaining):
    loop = asyncio.get_running_loop()
    slots = _get_slots()
    async with asyncio.timeout(remaining()):
        await slots.acquire()
        try:
            remaining()
            future = _executor.submit(copy_context().run, request)
        except BaseException:
            slots.release()
            raise
        # Release from the concurrent Future, never the cancelled asyncio
        # wrapper: abandoning an await must not admit more background work.
        future.add_done_callback(lambda done: _release_slot(loop, slots))
        return await asyncio.wrap_future(future)


def pinned_get(url, *, headers, timeout, max_response_bytes, validate_response, transport=None,
               before_send=None, admission_loop=None):
    """GET with the supplied headers plus Host, validating before reading the body.

    The API supplies its event loop to use the same 16 admission slots as
    licence requests. The standalone reports process submits to its own pool.
    Both use the same deadline for queueing, the optional policy check and I/O.
    before_send must return true to permit opening the stream.
    """
    @contextmanager
    def open_stream(client, _budget):
        client.headers.clear()
        if before_send is not None:
            remaining()
            if not before_send():
                raise OutboundRequestCancelled()
        with client.stream("GET", url, headers=headers, timeout=remaining(), follow_redirects=False) as response:
            validate_response(response)
            yield response

    request, remaining = _prepare_request(
        url, timeout, max_response_bytes, open_stream, transport=transport)
    if admission_loop is None:
        future = _executor.submit(copy_context().run, request)
    else:
        future = asyncio.run_coroutine_threadsafe(_run_in_slot(request, remaining), admission_loop)
    try:
        return future.result(timeout=remaining())
    except TimeoutError as exc:
        future.cancel()
        raise httpx.TimeoutException("Outbound request total deadline exceeded") from exc


async def pinned_async_request(
    method: str, url: str, *, timeout=5,
    max_response_bytes=_DEFAULT_MAX_RESPONSE_BYTES, public_only=False, **kwargs,
) -> httpx.Response:
    """Validate, pin and complete one request in the same worker thread.

    AsyncClient's asyncio DNS executor does not inherit the ContextVar pin.
    A synchronous Client resolves and connects in the thread holding the pin;
    a dedicated bounded pool keeps DNS and network I/O off the event loop and
    asyncio's shared executor. Requests wait for one of 16 slots within their
    total deadline, including admission, DNS and response buffering.
    The original URL is retained for Host, TLS SNI and certificate verification.
    Responses are streamed with a deadline check per chunk and a decoded body
    size limit (4 MiB by default, overridable by the caller), then returned buffered.

    Outside ITAR, retain the existing unpinned, proxy-aware egress policy.
    Callers accepting untrusted provider URLs can set public_only to require
    public DNS answers and disable proxies even outside ITAR. Validation and
    pinning still happen in the connecting worker.
    Redirects remain disabled so an upstream cannot change the destination.

    Cancellation/deadline cannot interrupt an in-progress synchronous operation.
    Its slot remains occupied until the worker actually exits; streaming checks
    stop a dribbling body on the next chunk, and blocking I/O has httpx timeouts.
    No late request starts after DNS/client setup consumes the deadline. A
    cancelled or timed-out license POST may still complete at the portal.
    Callers must reconcile portal state before retrying a mutation.
    """
    def open_stream(client, budget):
        return client.stream(method, url, timeout=budget, follow_redirects=False, **kwargs)

    cancelled = threading.Event()
    request, remaining = _prepare_request(
        url, timeout, max_response_bytes, open_stream, public_only=public_only, cancelled=cancelled)
    try:
        return await _run_in_slot(request, remaining)
    except TimeoutError as exc:
        cancelled.set()
        raise httpx.TimeoutException("Outbound request total deadline exceeded") from exc
    except asyncio.CancelledError:
        cancelled.set()
        raise
