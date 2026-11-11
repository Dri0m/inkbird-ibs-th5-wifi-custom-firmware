"""Display mode and forecast band text style."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MODE_FORECAST, MODE_PICTURE
from .entity import Th5Entity
from .runtime import Th5ConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: Th5ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([ModeSelect(entry.runtime_data, "mode"),
                        ClockTextSelect(entry.runtime_data, "clock_text")])


class ModeSelect(Th5Entity, SelectEntity):
    """Picture can be chosen once th5_display.show_image has set one."""

    _attr_options = [MODE_FORECAST, MODE_PICTURE]

    @property
    def current_option(self) -> str:
        return self.runtime.mode

    async def async_select_option(self, option: str) -> None:
        await self.runtime.async_set("mode", option)


class ClockTextSelect(Th5Entity, SelectEntity):
    """Greyscale keeps smooth text; monochrome reduces band refresh work."""

    _attr_options = ["greyscale", "monochrome"]
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def current_option(self) -> str:
        return "monochrome" if self.runtime.settings["monochrome"] else "greyscale"

    async def async_select_option(self, option: str) -> None:
        await self.runtime.async_set("monochrome", option == "monochrome")
