"""Print-floor entities (status schema 2) and their behaviour against an older ODIN (schema 1)."""
import pytest

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.odin.api import OdinSchemaError, validate_status

from .conftest import STATUS_URL
from .test_entities import _poll, _setup

FLOOR_ENTITIES = [
    "sensor.x1c_next_job",
    "sensor.x1c_waiting_on",
    "binary_sensor.x1c_out_of_service",
    "sensor.kobra_s1_next_job",
    "sensor.kobra_s1_waiting_on",
    "binary_sensor.kobra_s1_out_of_service",
    "sensor.odin_fleet_plates_done_today",
    "sensor.odin_fleet_plates_failed_today",
    "sensor.odin_fleet_queued_hours",
    "sensor.odin_fleet_queued_jobs_without_estimate",
]


@pytest.mark.parametrize("version", [1, 2])
def test_both_schema_versions_are_accepted(status, version):
    status["schema_version"] = version
    assert validate_status(status)["printers_by_id"][1]["name"] == "X1C"


@pytest.mark.parametrize("version", [0, 3, "2", None])
def test_other_schema_versions_are_refused(status, version):
    status["schema_version"] = version
    with pytest.raises(OdinSchemaError):
        validate_status(status)


async def test_schema_2_printer_entities(hass, aioclient_mock, status_v2):
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _setup(hass)

    job = hass.states.get("sensor.x1c_next_job")
    assert job.state == "Lid"
    assert job.attributes["job_id"] == 12
    assert job.attributes["held"] is False
    assert job.attributes["est_minutes"] == 95
    assert job.attributes["copies_on_plate"] == 4
    assert job.attributes["filaments"][0]["grams"] == 12.5
    assert job.attributes["plate_index"] == 1
    assert job.attributes["plate_count"] == 3
    assert job.attributes["ams_status"] == "mismatch"
    assert job.attributes["ams_slots"][0]["ok"] is False
    assert job.attributes["scheduled_start"] == "2026-11-09T03:00:00Z"

    assert hass.states.get("sensor.x1c_waiting_on").state == "nothing"
    assert hass.states.get("binary_sensor.x1c_out_of_service").state == "off"

    # A printer with no queued job: unknown state, no job attributes.
    idle = hass.states.get("sensor.kobra_s1_next_job")
    assert idle.state == STATE_UNKNOWN
    assert "job_id" not in idle.attributes

    waiting = hass.states.get("sensor.kobra_s1_waiting_on")
    assert waiting.state == "paused"
    assert waiting.attributes["reason"] == "Paused: possible spaghetti"
    assert waiting.attributes["since"] == "2026-11-09T00:40:00Z"
    assert waiting.attributes["pause_cause"] == "spaghetti"
    assert waiting.attributes["options"] == [
        "nothing", "paused", "out_of_service", "clear_plate", "blocked", "held"]
    assert hass.states.get("binary_sensor.kobra_s1_out_of_service").state == "off"


async def test_schema_2_fleet_entities(hass, aioclient_mock, status_v2):
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _setup(hass)
    assert hass.states.get("sensor.odin_fleet_plates_done_today").state == "7"
    failed = hass.states.get("sensor.odin_fleet_plates_failed_today")
    assert failed.state == "1"
    assert failed.attributes["state_class"] == "total"
    hours = hass.states.get("sensor.odin_fleet_queued_hours")
    assert float(hours.state) == 12.5
    assert hours.attributes["unit_of_measurement"] == "h"
    assert hass.states.get("sensor.odin_fleet_queued_jobs_without_estimate").state == "2"
    queue = hass.states.get("sensor.odin_fleet_queue_length")
    assert queue.state == "4"
    assert [job["name"] for job in queue.attributes["next_unassigned"]] == ["Gear"]


@pytest.mark.parametrize("count", [0, 3])
async def test_stopped_sensor_reads_the_count(hass, aioclient_mock, status_v2, count):
    status_v2["fleet"]["today"]["plates_stopped"] = count
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _setup(hass)
    stopped = hass.states.get("sensor.odin_fleet_plates_stopped_today")
    assert stopped.state == str(count)
    assert stopped.attributes["state_class"] == "total"
    assert hass.states.get("sensor.odin_fleet_plates_failed_today").state == "1"


@pytest.mark.parametrize("version", [1, 2])
async def test_stopped_sensor_is_unavailable_on_older_servers(hass, aioclient_mock, status, status_v2, version):
    payload = status if version == 1 else status_v2
    aioclient_mock.get(STATUS_URL, json=payload)
    await _setup(hass)
    assert hass.states.get("sensor.odin_fleet_plates_stopped_today").state == STATE_UNAVAILABLE
    payload["fleet"].setdefault("today", {})["plates_stopped"] = 3
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=payload)
    await _poll(hass)
    assert hass.states.get("sensor.odin_fleet_plates_stopped_today").state == "3"
    del payload["fleet"]["today"]["plates_stopped"]
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=payload)
    await _poll(hass)
    assert hass.states.get("sensor.odin_fleet_plates_stopped_today").state == STATE_UNAVAILABLE


@pytest.mark.parametrize("count", [0, 3])
async def test_did_not_complete_sensor_reads_the_count(hass, aioclient_mock, status_v2, count):
    status_v2["fleet"]["today"]["plates_did_not_complete"] = count
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _setup(hass)
    stopped = hass.states.get("sensor.odin_fleet_plates_that_did_not_complete_today")
    assert stopped.state == str(count)
    assert stopped.attributes["state_class"] == "total"
    assert hass.states.get("sensor.odin_fleet_plates_failed_today").state == "1"


@pytest.mark.parametrize("version", [1, 2])
async def test_did_not_complete_sensor_is_unavailable_on_older_servers(hass, aioclient_mock, status, status_v2, version):
    payload = status if version == 1 else status_v2
    aioclient_mock.get(STATUS_URL, json=payload)
    await _setup(hass)
    assert hass.states.get("sensor.odin_fleet_plates_that_did_not_complete_today").state == STATE_UNAVAILABLE
    payload["fleet"].setdefault("today", {})["plates_did_not_complete"] = 3
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=payload)
    await _poll(hass)
    assert hass.states.get("sensor.odin_fleet_plates_that_did_not_complete_today").state == "3"
    del payload["fleet"]["today"]["plates_did_not_complete"]
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=payload)
    await _poll(hass)
    assert hass.states.get("sensor.odin_fleet_plates_that_did_not_complete_today").state == STATE_UNAVAILABLE


async def test_maintenance_hold_shows_as_out_of_service(hass, aioclient_mock, status_v2):
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _setup(hass)
    printer = status_v2["printers"][1]
    printer["out_of_service"] = True
    printer["bench"] = {"state": "out_of_service", "reason": "Nozzle swap", "since": "2026-11-09T00:50:00Z",
                        "pause_cause": None}
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _poll(hass)
    assert hass.states.get("binary_sensor.kobra_s1_out_of_service").state == "on"
    waiting = hass.states.get("sensor.kobra_s1_waiting_on")
    assert waiting.state == "out_of_service"
    assert waiting.attributes["reason"] == "Nozzle swap"
    assert waiting.attributes["pause_cause"] is None


async def test_schema_1_reports_unknown_and_never_raises(hass, aioclient_mock, status):
    aioclient_mock.get(STATUS_URL, json=status)
    await _setup(hass)
    for entity_id in FLOOR_ENTITIES:
        assert hass.states.get(entity_id).state == STATE_UNKNOWN, entity_id
    assert "job_id" not in hass.states.get("sensor.x1c_next_job").attributes
    assert "reason" not in hass.states.get("sensor.x1c_waiting_on").attributes
    queue = hass.states.get("sensor.odin_fleet_queue_length")
    assert queue.state == "4"
    assert "next_unassigned" not in queue.attributes


async def test_upgrading_odin_fills_in_the_floor_entities(hass, aioclient_mock, status, status_v2):
    aioclient_mock.get(STATUS_URL, json=status)
    await _setup(hass)
    assert hass.states.get("sensor.x1c_next_job").state == STATE_UNKNOWN
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _poll(hass)
    assert hass.states.get("sensor.x1c_next_job").state == "Lid"
    assert hass.states.get("sensor.odin_fleet_plates_done_today").state == "7"


async def test_malformed_floor_fields_report_unknown(hass, aioclient_mock, status_v2):
    printer = status_v2["printers"][0]
    printer["next_job"] = "oops"
    printer["bench"] = {"state": "a_future_state"}
    printer["out_of_service"] = "yes"
    status_v2["fleet"].update({"today": [1], "queued_hours": "x", "queued_unestimated": None,
                               "next_unassigned": "x"})
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _setup(hass)
    for entity_id in ("sensor.x1c_next_job", "sensor.x1c_waiting_on", "binary_sensor.x1c_out_of_service",
                      "sensor.odin_fleet_plates_done_today", "sensor.odin_fleet_plates_failed_today",
                      "sensor.odin_fleet_queued_hours", "sensor.odin_fleet_queued_jobs_without_estimate"):
        assert hass.states.get(entity_id).state == STATE_UNKNOWN, entity_id
    queue = hass.states.get("sensor.odin_fleet_queue_length")
    assert queue.state == "4"
    assert "next_unassigned" not in queue.attributes


async def test_ams_humidity_is_the_wettest_unit_and_unknown_without_a_sensor(hass, aioclient_mock, status_v2):
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _setup(hass)
    wet = hass.states.get("sensor.x1c_ams_humidity")
    assert wet.state == "4" and wet.attributes["grade"] == "D" and wet.attributes["wettest_unit"] == 0
    assert [u["unit"] for u in wet.attributes["units"]] == [0, 1]
    none = hass.states.get("sensor.kobra_s1_ams_humidity")
    assert none.state == "unknown" and none.attributes["grade"] is None    # no sensor: unknown, never 0


async def test_ams_humidity_is_unknown_on_an_older_odin(hass, aioclient_mock, status):
    aioclient_mock.get(STATUS_URL, json=status)
    await _setup(hass)
    assert hass.states.get("sensor.x1c_ams_humidity").state == "unknown"


async def test_long_pause_feed_attributes_and_count(hass, aioclient_mock, status_v2):
    status_v2['fleet']['paused_long'] = 1
    status_v2['printers'][1]['bench'].update(paused_seconds=18000, paused_long=True, pause_cause='filament')
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _setup(hass)
    assert hass.states.get('sensor.odin_fleet_printers_paused_too_long').state == '1'
    waiting = hass.states.get('sensor.kobra_s1_waiting_on')
    assert waiting.state == 'paused'
    assert waiting.attributes['paused_seconds'] == 18000
    assert waiting.attributes['pause_cause'] == 'filament'


@pytest.mark.parametrize('version', [1, 2])
async def test_long_pause_sensors_unavailable_on_older_server(hass, aioclient_mock, status, status_v2, version):
    payload = status if version == 1 else status_v2
    aioclient_mock.get(STATUS_URL, json=payload)
    await _setup(hass)
    assert hass.states.get('binary_sensor.kobra_s1_paused_too_long').state == STATE_UNAVAILABLE
    assert hass.states.get('sensor.odin_fleet_printers_paused_too_long').state == STATE_UNAVAILABLE
