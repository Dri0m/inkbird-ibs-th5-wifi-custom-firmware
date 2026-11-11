"""Base entity: lives on the ESPHome device the entry drives.

Since HA 2026.8 a device belongs to one config entry, and a DeviceInfo with
the ESPHome device's MAC would create a second, separate device. Setting
`device_entry` (as helpers such as switch_as_x do) puts these entities on
the ESPHome device page itself, next to its readings.
"""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity

from .runtime import Th5Display


class Th5Entity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: Th5Display, key: str) -> None:
        self.runtime = runtime
        self._attr_translation_key = key
        self._attr_unique_id = f"{runtime.link.mac}-{key}"
        self.device_entry = dr.async_get(runtime.hass).async_get(runtime.link.device_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.async_add_listener(self._handle_runtime_update))

    @callback
    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()
