"""Integration 0.3.0: spool slots, next-job readiness, finish and plate-clear times, and the status times."""
from homeassistant.const import STATE_UNKNOWN

from .conftest import STATUS_URL
from .test_entities import _poll, _setup

NEW_ENTITIES = [
    "sensor.x1c_last_print_finished",
    "sensor.x1c_plate_cleared",
    "sensor.x1c_spools_loaded",
    "sensor.kobra_s1_last_print_finished",
    "sensor.kobra_s1_plate_cleared",
    "sensor.kobra_s1_spools_loaded",
]


async def test_spool_slots_are_the_attributes_of_one_sensor_per_printer(hass, aioclient_mock, status_next):
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _setup(hass)
    spools = hass.states.get("sensor.x1c_spools_loaded")
    assert spools.state == "2"  # slot 3 is empty
    assert [slot["slot"] for slot in spools.attributes["slots"]] == [1, 2, 3]
    assert spools.attributes["slots"][0] == {
        "slot": 1, "color_hex": "#1A1A1A", "name": "Galaxy Black", "material": "PLA",
        "remaining_percent": 64.0, "remaining_g": 640.0}
    assert spools.attributes["slots"][1]["remaining_g"] is None  # unknown stays unknown, never 0
    # One sensor per printer, not one entity per slot.
    assert [e for e in hass.states.async_entity_ids("sensor") if "slot" in e or "spool" in e] == [
        "sensor.x1c_spools_loaded", "sensor.kobra_s1_spools_loaded"]
    empty = hass.states.get("sensor.kobra_s1_spools_loaded")
    assert empty.state == "0" and empty.attributes["slots"] == []


async def test_next_job_carries_queue_depth_short_spools_and_ready(hass, aioclient_mock, status_next):
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _setup(hass)
    job = hass.states.get("sensor.x1c_next_job")
    assert job.state == "Lid"
    assert job.attributes["queued_after"] == 3
    assert job.attributes["short_spools"] == [{"slot": 2, "needed_g": 13.8, "remaining_g": 9.0}]
    assert job.attributes["ready"] is False
    assert job.attributes["ams_status"] == "mismatch"  # the 0.2.0 attributes are unchanged
    status_next["printers"][0]["next_job"].update({"short_spools": [], "ready": True, "queued_after": 0})
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _poll(hass)
    job = hass.states.get("sensor.x1c_next_job")
    assert job.attributes["ready"] is True and job.attributes["short_spools"] == []
    assert job.attributes["queued_after"] == 0


async def test_finish_and_plate_clear_times_are_timestamps(hass, aioclient_mock, status_next):
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _setup(hass)
    finished = hass.states.get("sensor.kobra_s1_last_print_finished")
    assert finished.state == "2026-11-08T20:15:00+00:00"
    assert finished.attributes["device_class"] == "timestamp"
    assert hass.states.get("sensor.kobra_s1_plate_cleared").state == "2026-11-08T20:20:00+00:00"
    # The X1C is printing and nothing was confirmed: unknown, not a made-up time.
    assert hass.states.get("sensor.x1c_last_print_finished").state == STATE_UNKNOWN
    assert hass.states.get("sensor.x1c_plate_cleared").state == STATE_UNKNOWN


async def test_status_carries_the_completed_and_error_times_and_follows_odin(hass, aioclient_mock, status_next):
    printer = status_next["printers"][0]
    printer.update({"status": "idle", "raw_state": "FINISH", "progress": None,
                    "last_error": {"code": "PRINT_FAILED", "message": "Nozzle clog", "at": "2026-11-08T21:00:00Z",
                                   "unresolved": False}})
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _setup(hass)
    status = hass.states.get("sensor.x1c_status")
    # A print completed after the recorded error: ODIN reports idle and the sensor says so.
    assert status.state == "idle"
    assert status.attributes["last_completed_at"] == "2026-11-08T22:10:00Z"
    assert status.attributes["last_error_at"] == "2026-11-08T21:00:00Z"
    assert hass.states.get("binary_sensor.x1c_needs_attention").state == "off"
    kobra = hass.states.get("sensor.kobra_s1_status")
    assert kobra.attributes["last_completed_at"] is None
    assert kobra.attributes["last_error_at"] == "2026-11-08T23:36:50Z"


async def test_an_older_odin_reports_unknown_for_the_new_entities(hass, aioclient_mock, status_v2):
    aioclient_mock.get(STATUS_URL, json=status_v2)
    await _setup(hass)
    for entity_id in NEW_ENTITIES:
        assert hass.states.get(entity_id).state == STATE_UNKNOWN, entity_id
    assert "slots" not in hass.states.get("sensor.x1c_spools_loaded").attributes
    assert "last_completed_at" not in hass.states.get("sensor.x1c_status").attributes
    job = hass.states.get("sensor.x1c_next_job")
    assert job.state == "Lid"
    assert job.attributes["queued_after"] is None and job.attributes["short_spools"] is None
    assert job.attributes["ready"] is None


async def test_malformed_new_fields_report_unknown(hass, aioclient_mock, status_next):
    printer = status_next["printers"][0]
    printer.update({"slots": "oops", "finished_at": 5, "plate_clear_confirmed_at": "not a time",
                    "last_completed_at": 7, "last_error_at": None})
    printer["next_job"].update({"queued_after": "many", "short_spools": "x", "ready": "yes"})
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _setup(hass)
    for entity_id in NEW_ENTITIES[:3]:
        assert hass.states.get(entity_id).state == STATE_UNKNOWN, entity_id
    job = hass.states.get("sensor.x1c_next_job")
    assert job.attributes["queued_after"] is None and job.attributes["short_spools"] is None
    assert job.attributes["ready"] is None
    status = hass.states.get("sensor.x1c_status")
    assert status.attributes["last_completed_at"] is None and status.attributes["last_error_at"] is None


async def test_the_integration_still_makes_one_status_call_per_poll(hass, aioclient_mock, status_next):
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _setup(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _poll(hass)
    assert aioclient_mock.call_count == 1
    assert str(aioclient_mock.mock_calls[0][1]) == STATUS_URL
