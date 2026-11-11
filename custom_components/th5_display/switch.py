"""Switches: 24-hour clock, maintenance (OTA window), picture top bar."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import Th5Entity
from .runtime import Th5ConfigEntry, Th5Display

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class Th5SwitchDescription(SwitchEntityDescription):
    setting: str


SWITCHES = (
    Th5SwitchDescription(key="clock_24h", translation_key="clock_24h", setting="clock_24h"),
    # At the next sync the device stays up (HOLD, 15 min) for an OTA; the
    # switch clears itself when that window ends. Turning it off while the
    # device is held ends the window at once.
    Th5SwitchDescription(key="maintenance", translation_key="maintenance", setting="maintenance"),
    # Picture mode: the live top bar (icons, clock, T/RH) above the picture.
    Th5SwitchDescription(key="top_bar", translation_key="top_bar", setting="top_bar",
                         entity_category=EntityCategory.CONFIG),
    Th5SwitchDescription(key="single_reset", translation_key="single_reset", setting="single_reset",
                         entity_category=EntityCategory.CONFIG),
)


async def async_setup_entry(hass: HomeAssistant, entry: Th5ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(Th5Switch(entry.runtime_data, d) for d in SWITCHES)


class Th5Switch(Th5Entity, SwitchEntity):
    entity_description: Th5SwitchDescription

    def __init__(self, runtime: Th5Display, description: Th5SwitchDescription) -> None:
        super().__init__(runtime, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return bool(self.runtime.settings[self.entity_description.setting])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.setting != "maintenance":
            return None
        return {"held_until": self.runtime.status.held_until if self.runtime.held else None}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_set(self.entity_description.setting, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_set(self.entity_description.setting, False)
