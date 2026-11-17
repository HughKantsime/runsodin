"""Plate-clear service, status attributes and immediate coordinator refresh."""
import pytest
from homeassistant.exceptions import HomeAssistantError
from .conftest import STATUS_URL, URL
from .test_entities import _setup, _poll


async def test_press_calls_odin_and_refreshes_state(hass, aioclient_mock, status_next):
    status_next['printers'][0]['plate_clear'] = {'needed': True, 'releases_job': None,
        'has_waiting_job': False, 'last_print_ended_at': '2026-11-08T22:10:00Z'}
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _setup(hass)
    assert hass.states.get('binary_sensor.x1c_plate_needs_clearing').state == 'on'
    assert hass.states.get('sensor.x1c_status').attributes['plate_clear']['needed'] is True
    aioclient_mock.clear_requests()
    status_next['printers'][0]['plate_clear']['needed'] = False
    aioclient_mock.get(STATUS_URL, json=status_next)
    aioclient_mock.post(URL + '/api/v1/printers/1/plate-cleared', json={'success': True, 'message': 'Plate clear recorded'})
    await hass.services.async_call('button', 'press', {'entity_id': 'button.x1c_plate_cleared'}, blocking=True)
    assert [r[0] for r in aioclient_mock.mock_calls] == ['POST', 'GET']
    assert hass.states.get('binary_sensor.x1c_plate_needs_clearing').state == 'off'
    assert hass.states.get('sensor.x1c_status').attributes['plate_clear']['needed'] is False


async def test_missing_write_permission_is_a_clear_error_without_retry(hass, aioclient_mock, status_next):
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _setup(hass)
    assert hass.states.get('button.x1c_plate_cleared') is not None
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL + '/api/v1/printers/1/plate-cleared', status=403)
    with pytest.raises(HomeAssistantError, match='write:printers.*printers.edit'):
        await hass.services.async_call('button', 'press', {'entity_id': 'button.x1c_plate_cleared'}, blocking=True)
    assert len(aioclient_mock.mock_calls) == 1


async def test_binary_sensor_follows_needed_and_old_feeds_are_unknown(hass, aioclient_mock, status_next):
    aioclient_mock.get(STATUS_URL, json=status_next)
    await _setup(hass)
    assert hass.states.get('binary_sensor.kobra_s1_plate_needs_clearing').state == 'unknown'
    for needed in (True, False):
        status_next['printers'][1]['plate_clear'] = {'needed': needed}
        aioclient_mock.clear_requests()
        aioclient_mock.get(STATUS_URL, json=status_next)
        await _poll(hass)
        assert hass.states.get('binary_sensor.kobra_s1_plate_needs_clearing').state == ('on' if needed else 'off')
