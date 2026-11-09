"""Config flow for ODIN: the user types the ODIN URL and an API key."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import OdinAuthError, OdinClient, OdinConnectionError, OdinSchemaError, normalize_url
from .const import (
    CONF_CAMERA_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


async def _validate(hass, url: str, api_key: str) -> tuple[dict[str, str], dict[str, Any] | None]:
    client = OdinClient(async_get_clientsession(hass), url, api_key)
    try:
        status = await client.async_get_status()
    except OdinAuthError as err:
        return {"base": "invalid_auth" if err.status == 401 else "missing_scopes"}, None
    except OdinSchemaError:
        return {"base": "unsupported"}, None
    except OdinConnectionError:
        return {"base": "cannot_connect"}, None
    return {}, status


class OdinConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = normalize_url(user_input[CONF_URL])
            await self.async_set_unique_id(url.lower())
            self._abort_if_unique_id_configured()
            errors, status = await _validate(self.hass, url, user_input[CONF_API_KEY])
            if not errors:
                options = {CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL}
                if user_input.get(CONF_CAMERA_TOKEN):
                    options[CONF_CAMERA_TOKEN] = user_input[CONF_CAMERA_TOKEN]
                return self.async_create_entry(
                    title=f"ODIN ({status.get('odin_version') or url})" if status else "ODIN",
                    data={CONF_URL: url, CONF_API_KEY: user_input[CONF_API_KEY]},
                    options=options,
                )
        schema = vol.Schema({
            vol.Required(CONF_URL, default=(user_input or {}).get(CONF_URL, "")): str,
            vol.Required(CONF_API_KEY): _SECRET,
            vol.Optional(CONF_CAMERA_TOKEN): _SECRET,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, _ = await _validate(self.hass, entry.data[CONF_URL], user_input[CONF_API_KEY])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): _SECRET}),
            errors=errors,
            description_placeholders={"url": entry.data[CONF_URL]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OdinOptionsFlow()


class OdinOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            options = {CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            if user_input.get(CONF_CAMERA_TOKEN):
                options[CONF_CAMERA_TOKEN] = user_input[CONF_CAMERA_TOKEN]
            return self.async_create_entry(data=options)
        current = self.config_entry.options
        schema = vol.Schema({
            vol.Required(CONF_SCAN_INTERVAL, default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)):
                NumberSelector(NumberSelectorConfig(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, step=1,
                                                    unit_of_measurement="s", mode=NumberSelectorMode.BOX)),
            # Leave empty to remove the camera token (and the camera entities).
            vol.Optional(CONF_CAMERA_TOKEN): _SECRET,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
