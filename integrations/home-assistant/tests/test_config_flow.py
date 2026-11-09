"""Config flow: URL + API key typed by the user, validated against ODIN."""
import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.data_entry_flow import FlowResultType

from custom_components.odin.const import CONF_CAMERA_TOKEN, CONF_SCAN_INTERVAL, DOMAIN

from .conftest import STATUS_URL, URL


async def _start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_user_flow_creates_entry(hass, aioclient_mock, status):
    aioclient_mock.get(STATUS_URL, json=status)
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL + "/", CONF_API_KEY: "odin_key", CONF_CAMERA_TOKEN: "odin_cam"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: URL, CONF_API_KEY: "odin_key"}
    assert result["options"] == {CONF_SCAN_INTERVAL: 20, CONF_CAMERA_TOKEN: "odin_cam"}
    assert aioclient_mock.mock_calls[0][3]["X-API-Key"] == "odin_key"


@pytest.mark.parametrize("kwargs,error", [
    ({"status": 401}, "invalid_auth"),
    ({"status": 403}, "missing_scopes"),
    ({"status": 404}, "unsupported"),
    ({"status": 500}, "cannot_connect"),
    ({"json": {"schema_version": 99}}, "unsupported"),
    ({"exc": TimeoutError()}, "cannot_connect"),
])
async def test_user_flow_errors(hass, aioclient_mock, kwargs, error):
    aioclient_mock.get(STATUS_URL, **kwargs)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API_KEY: "odin_key"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_duplicate_server_aborts(hass, aioclient_mock, status):
    aioclient_mock.get(STATUS_URL, json=status)
    for expected in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: URL, CONF_API_KEY: "odin_key"})
        assert result["type"] is expected
