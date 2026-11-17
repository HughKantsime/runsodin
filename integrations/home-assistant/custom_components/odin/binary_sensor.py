"""ODIN binary sensors: printer online (connectivity), needs attention (problem) and out of service."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OdinConfigEntry
from .coordinator import OdinCoordinator
from .entity import OdinPrinterEntity, track_printers


async def async_setup_entry(hass: HomeAssistant, entry: OdinConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entry.async_on_unload(track_printers(
        coordinator, async_add_entities,
        lambda pid: [OdinOnlineSensor(coordinator, pid), OdinProblemSensor(coordinator, pid),
                     OdinOutOfServiceSensor(coordinator, pid), OdinPlateNeedsClearingSensor(coordinator, pid),
                     OdinPausedLongSensor(coordinator, pid)],
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


class OdinOutOfServiceSensor(OdinPrinterEntity, BinarySensorEntity):
    """On while the printer is on a maintenance hold in ODIN (dispatch blocked, monitoring continues).

    Unknown on an ODIN older than status schema 2, which has no such field.
    """

    def __init__(self, coordinator: OdinCoordinator, printer_id: int) -> None:
        super().__init__(coordinator, printer_id, "out_of_service")

    @property
    def is_on(self) -> bool | None:
        printer = self.printer
        value = printer.get("out_of_service") if printer else None
        return value if isinstance(value, bool) else None


class OdinPlateNeedsClearingSensor(OdinPrinterEntity, BinarySensorEntity):
    def __init__(self, coordinator: OdinCoordinator, printer_id: int) -> None:
        super().__init__(coordinator, printer_id, "plate_needs_clearing")

    @property
    def is_on(self) -> bool | None:
        plate = (self.printer or {}).get("plate_clear")
        needed = plate.get("needed") if isinstance(plate, dict) else None
        return needed if isinstance(needed, bool) else None


class OdinPausedLongSensor(OdinPrinterEntity, BinarySensorEntity):
    """Unknown while offline or the pause time is unknown; unavailable on older ODIN."""
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: OdinCoordinator, printer_id: int) -> None:
        super().__init__(coordinator, printer_id, "paused_long")

    @property
    def available(self) -> bool:
        fleet = (self.coordinator.data or {}).get("fleet") or {}
        bench = (self.printer or {}).get("bench")
        supported = "paused_long" in fleet or (isinstance(bench, dict) and "paused_long" in bench)
        return super().available and supported

    @property
    def is_on(self) -> bool | None:
        printer = self.printer or {}
        if not printer.get("online"):
            return None
        bench = printer.get("bench")
        if not isinstance(bench, dict) or bench.get("state") != "paused":
            return False
        value = bench.get("paused_long")
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self):
        bench = (self.printer or {}).get("bench")
        if not isinstance(bench, dict) or bench.get("state") != "paused":
            return None
        seconds = bench.get("paused_seconds")
        return {"hours": seconds / 3600 if type(seconds) in (int, float) else None,
                "cause": bench.get("pause_cause"), "since": bench.get("since"), "reason": bench.get("reason")}
