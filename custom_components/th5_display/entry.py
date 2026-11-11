"""Set up and tear down a TH5 Display config entry."""
from __future__ import annotations

import shutil

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.storage import STORAGE_DIR, Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import CONF_DEVICE_ID, DOMAIN, STORAGE_VERSION
from .device import LinkError, async_resolve_link
from .runtime import Th5ConfigEntry, Th5Display
from .services import async_setup_services

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions (they look up their entry when called)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: Th5ConfigEntry) -> bool:
    try:
        link = async_resolve_link(hass, entry.data[CONF_DEVICE_ID])
    except LinkError as err:
        if err.reason == "device_not_found":
            raise ConfigEntryError(
                translation_domain=DOMAIN, translation_key="device_gone") from err
        # ESPHome not loaded yet, or the firmware lost th5_link: try again later.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="link_not_ready",
            translation_placeholders={"reason": err.reason}) from err
    session = er.async_get(hass).async_get(link.session_entity_id)
    if session is not None and session.disabled:
        # A disabled entity gets no states, so no session would ever start.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="link_not_ready",
            translation_placeholders={"reason": f"{link.session_entity_id} is disabled"})

    tz = (await dt_util.async_get_time_zone(hass.config.time_zone)
          or await dt_util.async_get_time_zone("UTC"))
    assert tz is not None
    # 0.2.6 replaced the "Picture badge" switch with "Picture top bar": drop
    # the old registry entry rather than leave it as "no longer provided".
    ent_reg = er.async_get(hass)
    if old := ent_reg.async_get_entity_id(Platform.SWITCH, DOMAIN, f"{link.mac}-badge"):
        ent_reg.async_remove(old)

    runtime = Th5Display(hass, entry, link, tz)
    await runtime.async_load()
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Th5ConfigEntry) -> bool:
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: Th5ConfigEntry) -> None:
    """Delete the stored settings, frames and picture with the entry."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    await hass.async_add_executor_job(
        shutil.rmtree, hass.config.path(STORAGE_DIR, DOMAIN, entry.entry_id), True)
