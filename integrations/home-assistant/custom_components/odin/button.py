"""Plate-clear confirmation through ODIN; never a direct printer connection."""
from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import OdinError
from .entity import OdinPrinterEntity, track_printers


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    # There is no active-token introspection read. /tokens lists the owner's
    # tokens by non-unique prefixes; /auth/me reports a role, not token scopes.
    # Let ODIN authorize every press and explain a refusal without retrying.
    entry.async_on_unload(track_printers(
        coordinator, async_add_entities, lambda pid: [OdinPlateClearedButton(coordinator, pid)],
    ))


class OdinPlateClearedButton(OdinPrinterEntity, ButtonEntity):
    def __init__(self, coordinator, printer_id):
        super().__init__(coordinator, printer_id, "plate_cleared")
        self._pressing = False

    async def async_press(self):
        if self._pressing:
            return
        self._pressing = True
        try:
            await self.coordinator.client.async_plate_cleared(self.printer_id)
            await self.coordinator.async_request_refresh()
        except OdinError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            self._pressing = False
