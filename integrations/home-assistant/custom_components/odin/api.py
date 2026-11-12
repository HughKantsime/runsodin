"""Small async client for ODIN's read-only Home Assistant status endpoint.

Home Assistant only ever talks to ODIN. It never opens a connection to a
printer: ODIN owns the printers' limited MQTT/RTSP slots.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT, STATUS_PATH, SUPPORTED_SCHEMA_VERSIONS


class OdinError(Exception):
    """Base error."""


class OdinConnectionError(OdinError):
    """ODIN could not be reached or answered with a server error."""


class OdinAuthError(OdinError):
    """The API key was refused (401) or lacks the required scopes (403)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"ODIN refused the API key (HTTP {status})")
        self.status = status


class OdinSchemaError(OdinError):
    """ODIN answered with a payload this integration does not understand."""


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


class OdinClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str) -> None:
        self._session = session
        self._url = normalize_url(url)
        self._api_key = api_key

    @property
    def url(self) -> str:
        return self._url

    async def async_get_status(self) -> dict[str, Any]:
        try:
            async with self._session.get(
                self._url + STATUS_PATH,
                headers={"X-API-Key": self._api_key, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT.total_seconds()),
            ) as response:
                if response.status in (401, 403):
                    raise OdinAuthError(response.status)
                if response.status == 404:
                    raise OdinSchemaError("This ODIN version has no Home Assistant status endpoint")
                if response.status >= 400:
                    raise OdinConnectionError(f"ODIN answered HTTP {response.status}")
                try:
                    data = await response.json()
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise OdinSchemaError("ODIN did not answer with JSON") from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise OdinConnectionError(str(err) or type(err).__name__) from err
        return validate_status(data)


def validate_status(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise OdinSchemaError("Unsupported ODIN status schema")
    if not isinstance(data.get("printers"), list) or not isinstance(data.get("fleet"), dict):
        raise OdinSchemaError("ODIN status is missing printers or fleet")
    printers = {}
    for printer in data["printers"]:
        if isinstance(printer, dict) and isinstance(printer.get("id"), int):
            printers[printer["id"]] = printer
    return {**data, "printers_by_id": printers}
