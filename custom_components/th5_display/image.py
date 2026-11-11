"""Preview: the frame the panel shows (or will show after the next sync).

The body is decoded from the very slot that is uploaded; in forecast mode
the MCU's live band is approximated with the clock and the indoor reading
from the ESPHome sensors (see preview.py)."""
from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .entity import Th5Entity
from .runtime import Th5ConfigEntry, Th5Display

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: Th5ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([PreviewImage(hass, entry.runtime_data)])


class PreviewImage(Th5Entity, ImageEntity):
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, runtime: Th5Display) -> None:
        Th5Entity.__init__(self, runtime, "preview")
        ImageEntity.__init__(self, hass)
        self._key: tuple | None = None
        self._update_key()

    def _band_key(self) -> tuple:
        b = self.runtime.preview_band()
        frame = self.runtime.current_frame()
        return (frame.frame_id if frame else None, b.units, b.temp, b.humidity, b.clock_24h)

    def _update_key(self) -> bool:
        """Bump image_last_updated when what the picture shows has changed."""
        key = self._band_key()
        if key == self._key:
            return False
        self._key = key
        self._attr_image_last_updated = dt_util.utcnow()
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        ids = [e for e in (self.runtime.link.temperature_entity_id,
                           self.runtime.link.humidity_entity_id) if e]
        if ids:
            self.async_on_remove(async_track_state_change_event(self.hass, ids, self._reading_changed))

    @callback
    def _reading_changed(self, _event: Event[EventStateChangedData]) -> None:
        if self._update_key():
            self.async_write_ha_state()

    @callback
    def _handle_runtime_update(self) -> None:
        self._update_key()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return await self.runtime.async_preview_png()
