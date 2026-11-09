"""Diagnostics for ODIN, with credentials redacted."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from . import OdinConfigEntry
from .const import CONF_CAMERA_TOKEN

TO_REDACT = {CONF_API_KEY, CONF_CAMERA_TOKEN}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: OdinConfigEntry) -> dict[str, Any]:
    data = dict(entry.runtime_data.data or {})
    data.pop("printers_by_id", None)
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT),
                  "options": async_redact_data(dict(entry.options), TO_REDACT)},
        "status": data,
    }
