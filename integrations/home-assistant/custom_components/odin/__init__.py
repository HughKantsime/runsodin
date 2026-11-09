"""The ODIN integration: read-only print farm status from an ODIN server."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OdinClient
from .coordinator import OdinCoordinator
from .entity import fleet_device

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.EVENT, Platform.CAMERA]

type OdinConfigEntry = ConfigEntry[OdinCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: OdinConfigEntry) -> bool:
    client = OdinClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_API_KEY])
    coordinator = OdinCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # Printer devices hang off the fleet device; create it before any platform.
    dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, **fleet_device(coordinator))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OdinConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: OdinConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
