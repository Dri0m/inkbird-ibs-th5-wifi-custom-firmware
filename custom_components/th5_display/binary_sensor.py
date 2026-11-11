"""Display up to date: the device's committed frame is the newest rendered one.

Known only at a sync (the device reports its frame id in the Session
sensor), so after a frame is sent this turns on at the following sync."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import Th5Entity
from .runtime import Th5ConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: Th5ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([UpToDateSensor(entry.runtime_data, "display_up_to_date")])


class UpToDateSensor(Th5Entity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        return self.runtime.display_up_to_date
