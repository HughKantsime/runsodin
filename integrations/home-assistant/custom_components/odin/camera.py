"""ODIN cameras through ODIN's MJPEG relay (never the printer's own RTSP).

Created only when a camera-only (read:cameras) token is configured. ODIN's
overlay relay refuses broader tokens, so the main API key never goes in a URL.
"""
from __future__ import annotations

import asyncio

import aiohttp

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_aiohttp_proxy_web, async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OdinConfigEntry
from .const import CONF_CAMERA_TOKEN
from .coordinator import OdinCoordinator
from .entity import OdinPrinterEntity, track_printers

SNAPSHOT_TIMEOUT = 10
SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024


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

    @property
    def available(self) -> bool:
        return super().available and bool((self.printer or {}).get("camera_available"))

    @property
    def _stream_url(self) -> str:
        return f"{self.coordinator.client.url}/api/overlay/{self.printer_id}/camera"

    def _request(self, timeout: aiohttp.ClientTimeout | None = None):
        session = async_get_clientsession(self.hass)
        return session.get(self._stream_url, params={"token": self._token}, timeout=timeout)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
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
