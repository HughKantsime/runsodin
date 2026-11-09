"""ODIN event entity: fires when a printer records a new error."""
from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OdinConfigEntry
from .coordinator import OdinCoordinator
from .entity import OdinPrinterEntity, track_printers

EVENT_ERROR = "error"


async def async_setup_entry(hass: HomeAssistant, entry: OdinConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entry.async_on_unload(track_printers(
        coordinator, async_add_entities, lambda pid: [OdinErrorEvent(coordinator, pid)]))


def _signature(error: Any) -> str | None:
    """An error's identity is when ODIN recorded it; an error without a time never fires."""
    if not isinstance(error, dict):
        return None
    at = error.get("at")
    return at if isinstance(at, str) and at else None


class OdinErrorEvent(OdinPrinterEntity, EventEntity):
    """The first poll after startup only records the current error; a newer one fires once.

    After a Home Assistant restart the current error is recorded again without
    firing, so a restart never re-announces an old error.
    """

    _attr_event_types = [EVENT_ERROR]

    def __init__(self, coordinator: OdinCoordinator, printer_id: int) -> None:
        super().__init__(coordinator, printer_id, "last_error")
        self._seen = _signature((self.printer or {}).get("last_error"))

    @callback
    def _handle_coordinator_update(self) -> None:
        printer = self.printer
        if printer is not None:
            error = printer.get("last_error")
            signature = _signature(error)
            if signature is not None and signature != self._seen:
                self._trigger_event(EVENT_ERROR, {
                    "code": error.get("code"),
                    "message": error.get("message"),
                    "at": error.get("at"),
                    "printer": printer.get("name"),
                    "status": printer.get("status"),
                })
            if signature is not None:
                self._seen = signature
        super()._handle_coordinator_update()
