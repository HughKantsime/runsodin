"""Base entities and dynamic printer discovery for ODIN."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OdinCoordinator

MANUFACTURERS = {
    "bambu": "Bambu Lab",
    "moonraker": "Klipper (Moonraker)",
    "prusalink": "Prusa",
    "elegoo": "Elegoo",
    "anycubic_lan": "Anycubic",
}


def fleet_device(coordinator: OdinCoordinator) -> DeviceInfo:
    entry = coordinator.config_entry
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_fleet")},
        name="ODIN fleet",
        manufacturer="ODIN",
        model="Print farm",
        sw_version=(coordinator.data or {}).get("odin_version"),
        configuration_url=coordinator.client.url,
    )


class OdinPrinterEntity(CoordinatorEntity[OdinCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OdinCoordinator, printer_id: int, key: str) -> None:
        super().__init__(coordinator)
        self.printer_id = printer_id
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{printer_id}_{key}"
        self._attr_translation_key = key
        printer = self.printer or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{printer_id}")},
            name=printer.get("name") or f"Printer {printer_id}",
            manufacturer=MANUFACTURERS.get(printer.get("api_type") or "", printer.get("api_type")),
            model=printer.get("model"),
            via_device=(DOMAIN, f"{entry.entry_id}_fleet"),
            configuration_url=f"{coordinator.client.url}/printers/{printer_id}",
        )

    @property
    def printer(self) -> dict[str, Any] | None:
        return ((self.coordinator.data or {}).get("printers_by_id") or {}).get(self.printer_id)

    @property
    def available(self) -> bool:
        return super().available and self.printer is not None


def track_printers(
    coordinator: OdinCoordinator,
    add_entities: Callable[[Iterable[Entity]], None],
    build: Callable[[int], Iterable[Entity]],
) -> Callable[[], None]:
    """Add entities for every printer now, and for printers that appear later."""
    known: set[int] = set()

    def discover() -> None:
        new = [pid for pid in ((coordinator.data or {}).get("printers_by_id") or {}) if pid not in known]
        if not new:
            return
        known.update(new)
        add_entities([entity for pid in new for entity in build(pid)])

    discover()
    return coordinator.async_add_listener(discover)
