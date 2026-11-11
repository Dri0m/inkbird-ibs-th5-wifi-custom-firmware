"""Redraw forecast: fetch the forecast and render new frames now."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import Th5Entity
from .runtime import Th5ConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: Th5ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([RedrawButton(entry.runtime_data, "redraw_forecast")])


class RedrawButton(Th5Entity, ButtonEntity):
    """The new frame is sent at the next sync, like everything else."""

    async def async_press(self) -> None:
        await self.runtime.async_refresh_forecast()
