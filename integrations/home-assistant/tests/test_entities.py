"""Entities built from ODIN's status payload; the error event fires only on change."""
from datetime import timedelta

from homeassistant.const import CONF_API_KEY, CONF_URL, STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.odin.const import CONF_SCAN_INTERVAL, DOMAIN

from .conftest import STATUS_URL, URL


async def _setup(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL.lower(), data={CONF_URL: URL, CONF_API_KEY: "odin_key"},
                            options=options or {CONF_SCAN_INTERVAL: 20})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(hass):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=21))
    await hass.async_block_till_done()


async def test_printer_and_fleet_entities(hass, aioclient_mock, status):
    aioclient_mock.get(STATUS_URL, json=status)
    await _setup(hass)
    assert hass.states.get("sensor.x1c_status").state == "printing"
    assert hass.states.get("sensor.x1c_progress").state == "42.5"
    assert hass.states.get("sensor.x1c_finish_time").state == "2026-11-09T02:30:00+00:00"
    assert hass.states.get("sensor.x1c_current_job").state == "Bracket"
    assert hass.states.get("sensor.x1c_current_layer").state == "12"
    assert hass.states.get("binary_sensor.x1c_online").state == "on"
    assert hass.states.get("binary_sensor.x1c_needs_attention").state == "off"
    assert hass.states.get("sensor.kobra_s1_status").state == "paused"
    assert hass.states.get("binary_sensor.kobra_s1_needs_attention").state == "on"
    assert hass.states.get("sensor.odin_fleet_printers_printing").state == "1"
    assert hass.states.get("sensor.odin_fleet_queue_length").state == "4"
    # No camera token configured: no camera entities.
    assert not hass.states.async_entity_ids("camera")
    devices = dr.async_get(hass).devices
    assert {d.name for d in devices.values()} >= {"X1C", "Kobra S1", "ODIN fleet"}


async def test_error_event_fires_only_on_a_new_error(hass, aioclient_mock, status):
    aioclient_mock.get(STATUS_URL, json=status)
    await _setup(hass)
    # The error already present at startup is recorded, not announced.
    assert hass.states.get("event.kobra_s1_error").state == "unknown"
    status["printers"][1]["last_error"] = {"code": "HMS_0300", "message": "Filament runout", "at": "2026-11-09T01:05:00Z"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=status)
    await _poll(hass)
    state = hass.states.get("event.kobra_s1_error")
    assert state.state != "unknown"
    assert state.attributes["event_type"] == "error"
    assert state.attributes["code"] == "HMS_0300"
    fired_at = state.state
    await _poll(hass)  # same error again: no new event
    assert hass.states.get("event.kobra_s1_error").state == fired_at


async def test_new_printer_is_added_and_removed_printer_goes_unavailable(hass, aioclient_mock, status):
    aioclient_mock.get(STATUS_URL, json=status)
    await _setup(hass)
    new = dict(status["printers"][0], id=7, name="P1S")
    status["printers"] = [status["printers"][1], new]
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, json=status)
    await _poll(hass)
    assert hass.states.get("sensor.p1s_status").state == "printing"
    assert hass.states.get("sensor.x1c_status").state == STATE_UNAVAILABLE


async def test_odin_unreachable_marks_entities_unavailable(hass, aioclient_mock, status):
    aioclient_mock.get(STATUS_URL, json=status)
    await _setup(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, status=502)
    await _poll(hass)
    assert hass.states.get("sensor.x1c_status").state == STATE_UNAVAILABLE


async def test_camera_entities_need_a_camera_token_and_a_camera(hass, aioclient_mock, status):
    aioclient_mock.get(STATUS_URL, json=status)
    await _setup(hass, {CONF_SCAN_INTERVAL: 20, "camera_token": "odin_cam"})
    assert hass.states.async_entity_ids("camera") == ["camera.x1c_camera"]


async def test_diagnostics_redact_credentials(hass, aioclient_mock, status):
    from custom_components.odin.diagnostics import async_get_config_entry_diagnostics
    aioclient_mock.get(STATUS_URL, json=status)
    entry = await _setup(hass, {CONF_SCAN_INTERVAL: 20, "camera_token": "odin_cam"})
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "odin_key" not in text and "odin_cam" not in text
    assert diag["status"]["fleet"]["queue_length"] == 4
