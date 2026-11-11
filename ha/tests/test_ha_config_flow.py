"""Config flow: device checks, weather checks, duplicates, reconfigure."""
from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from ha_fixtures import DOMAIN, MAC, NODE, WEATHER, FakeTh5

import pytest

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def _start(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return result


async def test_user_flow_creates_entry(hass: HomeAssistant, th5: FakeTh5, weather) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "device_id": th5.device_id, "weather_entity": WEATHER, "location": " Oslo "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Inkbird TH5"
    assert result["data"] == {"device_id": th5.device_id, "weather_entity": WEATHER,
                              "location": "Oslo"}
    assert result["result"].unique_id == MAC
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_location_defaults_to_home_name(hass: HomeAssistant, th5: FakeTh5, weather) -> None:
    hass.config.location_name = "Kraví hora"
    result = await _start(hass)
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == "location")
    assert key.description["suggested_value"] == "Kraví hora"


async def test_rejects_device_without_th5_link(hass: HomeAssistant, th5: FakeTh5, weather) -> None:
    hass.services.async_remove("esphome", f"{NODE}_th5_frame")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "device_id": th5.device_id, "weather_entity": WEATHER, "location": "Oslo"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"device_id": "no_th5_link"}


async def test_rejects_non_esphome_device(hass: HomeAssistant, th5: FakeTh5, weather) -> None:
    other = MockConfigEntry(domain="hue")
    other.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("hue", "lamp")}, name="Lamp")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "device_id": dev.id, "weather_entity": WEATHER, "location": "Oslo"})
    assert result["errors"] == {"device_id": "not_esphome"}


async def test_rejects_weather_without_hourly(hass: HomeAssistant, th5: FakeTh5, weather) -> None:
    hass.states.async_set("weather.daily_only", "sunny", {"supported_features": 1})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "device_id": th5.device_id, "weather_entity": "weather.daily_only", "location": "Oslo"})
    assert result["errors"] == {"weather_entity": "no_hourly_forecast"}
    # and recovers on the same form
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "device_id": th5.device_id, "weather_entity": WEATHER, "location": "Oslo"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_duplicate_device_aborts(hass: HomeAssistant, th5: FakeTh5, weather, entry) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "device_id": th5.device_id, "weather_entity": WEATHER, "location": "Oslo"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure(hass: HomeAssistant, th5: FakeTh5, weather, entry) -> None:
    hass.states.async_set("weather.other", "sunny", {"supported_features": 2})
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "weather_entity": "weather.other", "location": "Bergen"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.data["weather_entity"] == "weather.other"
    assert entry.data["location"] == "Bergen"
    assert entry.data["device_id"] == th5.device_id
