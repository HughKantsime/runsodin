"""ODIN binary sensors: printer online (connectivity) and needs attention (problem)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OdinConfigEntry
from .coordinator import OdinCoordinator
from .entity import OdinPrinterEntity, track_printers


async def async_setup_entry(hass: HomeAssistant, entry: OdinConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entry.async_on_unload(track_printers(
        coordinator, async_add_entities,
        lambda pid: [OdinOnlineSensor(coordinator, pid), OdinProblemSensor(coordinator, pid)],
    ))


class OdinOnlineSensor(OdinPrinterEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: OdinCoordinator, printer_id: int) -> None:
        super().__init__(coordinator, printer_id, "online")

    @property
    def is_on(self) -> bool | None:
        printer = self.printer
        return bool(printer.get("online")) if printer else None


class OdinProblemSensor(OdinPrinterEntity, BinarySensorEntity):
    """On while the printer reports an error state or an active HMS entry (even when paused)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: OdinCoordinator, printer_id: int) -> None:
        super().__init__(coordinator, printer_id, "problem")

    @property
    def is_on(self) -> bool | None:
        printer = self.printer
        if not printer:
            return None
        return printer.get("status") == "error" or bool(printer.get("active_errors"))
