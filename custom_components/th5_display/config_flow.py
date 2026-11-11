"""Config flow: pick the ESPHome TH5, a weather entity and the place name."""
from __future__ import annotations

from typing import Any

import probatio
from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN, WeatherEntityFeature
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
)

from .const import (
    CONF_DEVICE_ID,
    CONF_LOCATION,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    ESPHOME_DOMAIN,
)
from .device import LinkError, async_resolve_link

_WEATHER_SCHEMA = {
    probatio.Required(CONF_WEATHER_ENTITY): EntitySelector(
        EntitySelectorConfig(domain=WEATHER_DOMAIN)),
    probatio.Required(CONF_LOCATION): TextSelector(),
}

USER_SCHEMA = probatio.Schema({
    probatio.Required(CONF_DEVICE_ID): DeviceSelector(
        DeviceSelectorConfig(integration=ESPHOME_DOMAIN)),
    **_WEATHER_SCHEMA,
})
RECONFIGURE_SCHEMA = probatio.Schema(_WEATHER_SCHEMA)


class Th5DisplayConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per TH5, keyed by its MAC address."""

    VERSION = 1

    def _weather_error(self, entity_id: str) -> str | None:
        """None if the entity exists and offers an hourly forecast."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return "weather_not_found"
        if not int(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)) & WeatherEntityFeature.FORECAST_HOURLY:
            return "no_hourly_forecast"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                link = async_resolve_link(self.hass, user_input[CONF_DEVICE_ID])
            except LinkError as err:
                errors[CONF_DEVICE_ID] = err.reason
            else:
                await self.async_set_unique_id(link.mac)
                self._abort_if_unique_id_configured()
                if err_key := self._weather_error(user_input[CONF_WEATHER_ENTITY]):
                    errors[CONF_WEATHER_ENTITY] = err_key
                else:
                    return self.async_create_entry(
                        title=link.name,
                        data={
                            CONF_DEVICE_ID: link.device_id,
                            CONF_WEATHER_ENTITY: user_input[CONF_WEATHER_ENTITY],
                            CONF_LOCATION: user_input[CONF_LOCATION].strip(),
                        },
                    )
        suggested = user_input or {CONF_LOCATION: self.hass.config.location_name}
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, suggested),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the weather entity or the place name; the device stays."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if err_key := self._weather_error(user_input[CONF_WEATHER_ENTITY]):
                errors[CONF_WEATHER_ENTITY] = err_key
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_WEATHER_ENTITY: user_input[CONF_WEATHER_ENTITY],
                        CONF_LOCATION: user_input[CONF_LOCATION].strip(),
                    },
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                RECONFIGURE_SCHEMA, user_input or entry.data),
            errors=errors,
        )
