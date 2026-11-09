"""ODIN sensors: per-printer status and progress, plus fleet counts."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import OdinConfigEntry
from .const import PRINTER_STATUSES
from .coordinator import OdinCoordinator
from .entity import OdinPrinterEntity, fleet_device, track_printers


def _timestamp(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


def _status(printer: dict[str, Any]) -> str:
    status = printer.get("status")
    return status if status in PRINTER_STATUSES else "unknown"


@dataclass(frozen=True, kw_only=True)
class OdinSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]


PRINTER_SENSORS: tuple[OdinSensorDescription, ...] = (
    OdinSensorDescription(key="status", device_class=SensorDeviceClass.ENUM, options=PRINTER_STATUSES,
                          value=_status),
    OdinSensorDescription(key="progress", native_unit_of_measurement=PERCENTAGE,
                          state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                          value=lambda p: p.get("progress")),
    OdinSensorDescription(key="eta", device_class=SensorDeviceClass.TIMESTAMP,
                          value=lambda p: _timestamp(p.get("eta"))),
    OdinSensorDescription(key="remaining_time", device_class=SensorDeviceClass.DURATION,
                          native_unit_of_measurement=UnitOfTime.MINUTES, suggested_display_precision=0,
                          value=lambda p: p.get("remaining_minutes")),
    OdinSensorDescription(key="job_name", value=lambda p: p.get("job_name")),
    OdinSensorDescription(key="current_layer", state_class=SensorStateClass.MEASUREMENT,
                          value=lambda p: p.get("current_layer")),
    OdinSensorDescription(key="total_layers", value=lambda p: p.get("total_layers")),
    OdinSensorDescription(key="active_errors", state_class=SensorStateClass.MEASUREMENT,
                          value=lambda p: p.get("active_errors")),
)

FLEET_SENSORS: tuple[OdinSensorDescription, ...] = (
    OdinSensorDescription(key="fleet_printing", state_class=SensorStateClass.MEASUREMENT,
                          value=lambda f: f.get("printing")),
    OdinSensorDescription(key="fleet_paused", state_class=SensorStateClass.MEASUREMENT,
                          value=lambda f: f.get("paused")),
    OdinSensorDescription(key="fleet_error", state_class=SensorStateClass.MEASUREMENT,
                          value=lambda f: f.get("error")),
    OdinSensorDescription(key="fleet_offline", state_class=SensorStateClass.MEASUREMENT,
                          value=lambda f: f.get("offline")),
    OdinSensorDescription(key="queue_length", state_class=SensorStateClass.MEASUREMENT,
                          value=lambda f: f.get("queue_length")),
)


async def async_setup_entry(hass: HomeAssistant, entry: OdinConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(OdinFleetSensor(coordinator, description) for description in FLEET_SENSORS)
    entry.async_on_unload(track_printers(
        coordinator, async_add_entities,
        lambda pid: [OdinPrinterSensor(coordinator, pid, description) for description in PRINTER_SENSORS],
    ))


class OdinPrinterSensor(OdinPrinterEntity, SensorEntity):
    entity_description: OdinSensorDescription

    def __init__(self, coordinator: OdinCoordinator, printer_id: int, description: OdinSensorDescription) -> None:
        super().__init__(coordinator, printer_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        printer = self.printer
        return self.entity_description.value(printer) if printer else None


class OdinFleetSensor(CoordinatorEntity[OdinCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: OdinSensorDescription

    def __init__(self, coordinator: OdinCoordinator, description: OdinSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._attr_translation_key = description.key
        self._attr_device_info = fleet_device(coordinator)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value((self.coordinator.data or {}).get("fleet") or {})
