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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import OdinConfigEntry
from .const import BENCH_STATES, PRINTER_STATUSES
from .coordinator import OdinCoordinator
from .entity import OdinPrinterEntity, fleet_device, track_printers


def _timestamp(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


def _status(printer: dict[str, Any]) -> str:
    status = printer.get("status")
    return status if status in PRINTER_STATUSES else "unknown"


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _hours(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else round(number, 1)


def _today(fleet: dict[str, Any], key: str) -> int | float | None:
    today = fleet.get("today")
    return _number(today.get(key)) if isinstance(today, dict) else None


def _next_job(printer: dict[str, Any]) -> str | None:
    job = printer.get("next_job")
    name = job.get("name") if isinstance(job, dict) else None
    return name if isinstance(name, str) and name else None


def _next_job_attrs(printer: dict[str, Any]) -> dict[str, Any] | None:
    job = printer.get("next_job")
    if not isinstance(job, dict):
        return None
    plate = job.get("plate") if isinstance(job.get("plate"), dict) else {}
    ams = job.get("ams_check") if isinstance(job.get("ams_check"), dict) else {}
    filaments = job.get("filaments")
    slots = ams.get("slots")
    return {
        "job_id": job.get("id"),
        "held": job.get("held"),
        "est_minutes": job.get("est_minutes"),
        "copies_on_plate": job.get("copies_on_plate"),
        "filaments": filaments if isinstance(filaments, list) else None,
        "plate_index": plate.get("index"),
        "plate_count": plate.get("count"),
        "ams_status": ams.get("status"),
        "ams_slots": slots if isinstance(slots, list) else None,
        "scheduled_start": job.get("scheduled_start"),
    }


def _waiting_on(printer: dict[str, Any]) -> str | None:
    """Unknown on an older ODIN (no bench key) or an unrecognised state; "nothing" for a null bench."""
    if "bench" not in printer:
        return None
    bench = printer["bench"]
    if bench is None:
        return "nothing"
    if not isinstance(bench, dict):
        return None
    state = bench.get("state")
    if state is None:
        return "nothing"
    return state if state in BENCH_STATES else None


def _waiting_on_attrs(printer: dict[str, Any]) -> dict[str, Any] | None:
    bench = printer.get("bench")
    if not isinstance(bench, dict):
        return None
    return {"reason": bench.get("reason"), "since": bench.get("since"), "pause_cause": bench.get("pause_cause")}


def _ams_humidity(printer: dict[str, Any]) -> int | None:
    """The wettest AMS unit's humidity index: 1 (dry) to 5 (wet), as on the printer's A-E scale.
    Unknown on an older ODIN, with no AMS, or when no unit has a humidity sensor."""
    units = printer.get("ams")
    if not isinstance(units, list):
        return None
    values = [u.get("humidity_index") for u in units if isinstance(u, dict) and isinstance(u.get("humidity_index"), int)]
    return max(values) if values else None


def _ams_humidity_attrs(printer: dict[str, Any]) -> dict[str, Any] | None:
    units = printer.get("ams")
    if not isinstance(units, list) or not units:
        return None
    wettest = max((u for u in units if isinstance(u, dict) and isinstance(u.get("humidity_index"), int)),
                  key=lambda u: u["humidity_index"], default=None)
    return {"grade": wettest.get("grade") if wettest else None,
            "wettest_unit": wettest.get("unit") if wettest else None,
            "units": [u for u in units if isinstance(u, dict)]}


def _next_unassigned_attrs(fleet: dict[str, Any]) -> dict[str, Any] | None:
    jobs = fleet.get("next_unassigned")
    return {"next_unassigned": jobs} if isinstance(jobs, list) else None


@dataclass(frozen=True, kw_only=True)
class OdinSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]
    attrs: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None


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
    OdinSensorDescription(key="next_job", value=_next_job, attrs=_next_job_attrs),
    OdinSensorDescription(key="ams_humidity", state_class=SensorStateClass.MEASUREMENT,
                          value=_ams_humidity, attrs=_ams_humidity_attrs),
    OdinSensorDescription(key="waiting_on", device_class=SensorDeviceClass.ENUM, options=BENCH_STATES,
                          value=_waiting_on, attrs=_waiting_on_attrs),
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
                          value=lambda f: f.get("queue_length"), attrs=_next_unassigned_attrs),
    OdinSensorDescription(key="plates_done_today", state_class=SensorStateClass.TOTAL,
                          value=lambda f: _today(f, "plates_done")),
    OdinSensorDescription(key="plates_failed_today", state_class=SensorStateClass.TOTAL,
                          value=lambda f: _today(f, "plates_failed")),
    OdinSensorDescription(key="queued_hours", device_class=SensorDeviceClass.DURATION,
                          native_unit_of_measurement=UnitOfTime.HOURS, state_class=SensorStateClass.MEASUREMENT,
                          suggested_display_precision=1, value=lambda f: _hours(f.get("queued_hours"))),
    OdinSensorDescription(key="queued_unestimated", state_class=SensorStateClass.MEASUREMENT,
                          value=lambda f: _number(f.get("queued_unestimated"))),
)


async def async_setup_entry(hass: HomeAssistant, entry: OdinConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = self.entity_description.attrs
        printer = self.printer
        return attrs(printer) if attrs and printer else None


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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = self.entity_description.attrs
        return attrs((self.coordinator.data or {}).get("fleet") or {}) if attrs else None
