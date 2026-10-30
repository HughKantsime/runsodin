"""Shared bounded MJPEG transport for authenticated cameras and OBS overlays.

The pool is per API worker: 3 streams per user/printer and 8 per printer across
both routes by default. Overlay callers additionally cap each token at 3.
Authorization and config sync callbacks own short sessions and run in threads.
"""
import asyncio
import logging
import time as _time
import weakref

import anyio
import httpx
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from modules.printers.route_utils import mjpeg_stream_name
from modules.printers.go2rtc_access import go2rtc_api_url, go2rtc_client_kwargs

log = logging.getLogger("odin.api")
STREAMS = []  # oldest first; only modified on the API event loop


def claim_stream(entries, viewer, printer_id, *, per_user=3, per_printer=8, per_token=None, label="camera"):
    """Admit a stream or raise 429. Privileged viewers evict the oldest unprivileged one."""
    import asyncio
    same_token = [e for e in entries if e["token_id"] == viewer["token_id"]]
    same_user = [e for e in entries if e["printer_id"] == printer_id and e["user_id"] == viewer["user_id"]]
    on_printer = [e for e in entries if e["printer_id"] == printer_id]
    if (per_token is not None and len(same_token) >= per_token) or len(same_user) >= per_user:
        raise HTTPException(status_code=429, detail=f"Too many open {label} streams")
    if len(on_printer) >= per_printer:
        victims = [e for e in on_printer if not e["privileged"]] if viewer["privileged"] else []
        if not victims:
            raise HTTPException(status_code=429, detail=f"Too many open {label} streams")
        victim = victims[0]  # oldest
        victim["evicted"].set()
        release_stream(entries, victim)
    entry = {**viewer, "printer_id": printer_id, "evicted": asyncio.Event()}
    entries.append(entry)
    return entry


def release_stream(entries, entry):
    if entry in entries:
        entries.remove(entry)


async def open_mjpeg(printer_id, *, check, sync, claim, release, revalidate_seconds=30):
    """Authorize, admit, open upstream and relay without retaining a DB session."""
    viewer = await run_in_threadpool(check)
    entry = claim(viewer, printer_id)
    stream_name = mjpeg_stream_name(printer_id)
    client = upstream = None
    closed = []

    async def close_once():
        if closed:
            return
        closed.append(True)
        release(entry)
        # ASGI disconnect cancellation must still release both HTTP resources.
        with anyio.CancelScope(shield=True):
            try:
                if upstream is not None:
                    await upstream.aclose()
            finally:
                if client is not None:
                    await client.aclose()

    try:
        # Same rule as the WebRTC route: sync only when go2rtc answers and lacks the
        # stream; an unreachable go2rtc is reported, never "fixed" by a restart.
        try:
            async with httpx.AsyncClient(trust_env=False, **go2rtc_client_kwargs()) as probe:
                listed = await probe.get(f"{go2rtc_api_url()}/api/streams", timeout=2.0)
            known = listed.json() if listed.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            raise HTTPException(status_code=502, detail="go2rtc is not reachable")
        if isinstance(known, dict) and stream_name not in known:
            await run_in_threadpool(sync)
        client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(10.0, read=30.0), **go2rtc_client_kwargs())
        try:
            upstream = await client.send(client.build_request("GET", f"{go2rtc_api_url()}/api/stream.mjpeg", params={"src": stream_name}), stream=True)
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="go2rtc is not reachable")
        content_type = upstream.headers.get("content-type", "")
        if upstream.status_code != 200 or content_type.split(";", 1)[0].lower() != "multipart/x-mixed-replace":
            raise HTTPException(status_code=502, detail="Camera stream unavailable")
    except BaseException:
        await close_once()
        raise

    async def relay():
        # Each upstream read races the eviction event and the revalidation clock:
        # an evicted or withdrawn stream ends at once, even while the camera is
        # stalled between frames.
        next_check = _time.monotonic() + revalidate_seconds
        chunks = upstream.aiter_raw().__aiter__()
        evicted = asyncio.ensure_future(entry["evicted"].wait())
        pending = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(chunks.__anext__())
                timeout = max(0.0, next_check - _time.monotonic())
                await asyncio.wait({pending, evicted}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if evicted.done():
                    log.info("Camera stream for printer %s ended: slot given to a printer operator", printer_id)
                    return
                if _time.monotonic() >= next_check:
                    try:
                        current = await run_in_threadpool(check)
                    except HTTPException:
                        log.info("Camera stream for printer %s ended: authorization withdrawn", printer_id)
                        return
                    # Rights on the printer can change: a demoted viewer becomes evictable.
                    entry["privileged"] = current["privileged"]
                    next_check = _time.monotonic() + revalidate_seconds
                if pending.done():
                    try:
                        chunk = pending.result()
                    except StopAsyncIteration:
                        return
                    pending = None
                    yield chunk
        except httpx.HTTPError:
            return
        finally:
            for task in (pending, evicted):
                if task is not None and not task.done():
                    task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*(task for task in (pending, evicted) if task is not None), return_exceptions=True)
                await close_once()

    body = relay()
    loop = asyncio.get_running_loop()

    def never_iterated():
        # The body was dropped without running (client gone before the body):
        # free the slot and close the upstream response and client on the loop.
        release(entry)
        if not closed and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: loop.create_task(close_once()))
    weakref.finalize(body, never_iterated)
    return StreamingResponse(body, media_type=content_type,
                             headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
