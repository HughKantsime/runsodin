"""ODIN cameras through ODIN's MJPEG relay (never the printer's own RTSP).

Created only when a camera-only (read:cameras) token is configured. ODIN's
overlay relay refuses broader tokens, so the main API key never goes in a URL.
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_aiohttp_proxy_web, async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OdinConfigEntry
from .const import CONF_CAMERA_TOKEN
from .coordinator import OdinCoordinator
from .entity import OdinPrinterEntity, track_printers

# A camera relayed from UniFi Protect (rtspx) waits for a keyframe: first frames of 5-6 s were seen.
SNAPSHOT_TIMEOUT = 20
SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024
SNAPSHOT_RECHECK_SECONDS = 3600  # an older ODIN is asked for the snapshot route again hourly


async def async_setup_entry(hass: HomeAssistant, entry: OdinConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    token = entry.options.get(CONF_CAMERA_TOKEN)
    if not token:
        return
    coordinator = entry.runtime_data
    entry.async_on_unload(track_printers(
        coordinator, async_add_entities,
        lambda pid: [OdinCamera(coordinator, pid, token)]
        if (coordinator.data["printers_by_id"].get(pid) or {}).get("camera_available") else [],
    ))


def first_jpeg(buffer: bytes) -> bytes | None:
    start = buffer.find(b"\xff\xd8")
    if start < 0:
        return None
    end = buffer.find(b"\xff\xd9", start + 2)
    return buffer[start:end + 2] if end >= 0 else None


class OdinCamera(OdinPrinterEntity, Camera):
    def __init__(self, coordinator: OdinCoordinator, printer_id: int, token: str) -> None:
        OdinPrinterEntity.__init__(self, coordinator, printer_id, "camera")
        Camera.__init__(self)
        self._token = token
        self._snapshot_route: bool | None = None  # None = not yet known
        self._snapshot_checked = 0.0

    @property
    def available(self) -> bool:
        return super().available and bool((self.printer or {}).get("camera_available"))

    @property
    def _stream_url(self) -> str:
        return f"{self.coordinator.client.url}/api/overlay/{self.printer_id}/camera"

    @property
    def _snapshot_url(self) -> str:
        return f"{self.coordinator.client.url}/api/overlay/{self.printer_id}/snapshot"

    def _request(self, timeout: aiohttp.ClientTimeout | None = None, url: str | None = None):
        session = async_get_clientsession(self.hass)
        return session.get(url or self._stream_url, params={"token": self._token}, timeout=timeout)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """One still. ODIN 2.2.0+ serves a single JPEG that opens no stream; an older
        server has no such route (404), so the first frame of the stream is used instead."""
        if self._snapshot_route is not False or time.monotonic() - self._snapshot_checked > SNAPSHOT_RECHECK_SECONDS:
            try:
                async with self._request(aiohttp.ClientTimeout(total=SNAPSHOT_TIMEOUT), self._snapshot_url) as response:
                    if response.status == 200:
                        if (getattr(response, "content_length", None) or 0) > SNAPSHOT_MAX_BYTES:
                            return None
                        frame = await response.read()
                        if frame.startswith(b"\xff\xd8") and len(frame) <= SNAPSHOT_MAX_BYTES:
                            self._snapshot_route = True   # only a real JPEG proves the route exists
                            return frame
                        if self._snapshot_route:
                            return None
                        # A 200 that is not a picture is an older ODIN's page fallback or a proxy page.
                    elif response.status != 404 or self._snapshot_route:
                        return None
                    else:
                        # ODIN 2.2.0+ answers these two itself; any other 404 (the framework's
                        # "Not Found", or a proxy's page) means the route does not exist here.
                        try:
                            detail = (await response.json(content_type=None)).get("detail")
                        except (ValueError, AttributeError, aiohttp.ClientError):
                            detail = None
                        if detail in ("Printer not found", "Camera not found"):
                            return None
                    self._snapshot_route = False          # use the stream's first frame...
                    self._snapshot_checked = time.monotonic()  # ...and look again later (server upgrades)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return None
        buffer = b""
        try:
            async with self._request(aiohttp.ClientTimeout(total=SNAPSHOT_TIMEOUT)) as response:
                if response.status != 200:
                    return None
                async for chunk in response.content.iter_chunked(65536):
                    buffer += chunk
                    frame = first_jpeg(buffer)
                    if frame or len(buffer) > SNAPSHOT_MAX_BYTES:
                        return frame
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        return first_jpeg(buffer)

    async def handle_async_mjpeg_stream(self, request):
        stream = self._request()
        return await async_aiohttp_proxy_web(self.hass, request, stream)
