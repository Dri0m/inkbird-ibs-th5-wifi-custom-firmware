"""Numeric settings: sync interval, clock period, full refresh, offsets.

Each is stored in HA and sent at the next sync (th5_config)."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import Th5Entity
from .runtime import Th5ConfigEntry, Th5Display
from .session import RANGES

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class Th5NumberDescription(NumberEntityDescription):
    setting: str
    integer: bool = True


def _desc(key: str, setting: str, step: float, unit: str | None, *,
          integer: bool = True, mode: NumberMode = NumberMode.BOX,
          category: EntityCategory | None = None) -> Th5NumberDescription:
    lo, hi = RANGES[setting]
    return Th5NumberDescription(
        key=key, translation_key=key, setting=setting, integer=integer,
        native_min_value=lo, native_max_value=hi, native_step=step,
        native_unit_of_measurement=unit, mode=mode, entity_category=category)


NUMBERS = (
    _desc("sync_interval", "sync_min", 1, UnitOfTime.MINUTES),
    _desc("clock_period", "clock_min", 1, UnitOfTime.MINUTES),
    _desc("full_refresh", "full_n", 1, None, category=EntityCategory.CONFIG),
    # No temperature device class: HA would convert an offset like an absolute
    # temperature for imperial users (1 degC offset != 33.8 degF).
    _desc("temperature_offset", "t_off", 0.1, UnitOfTemperature.CELSIUS, integer=False,
          category=EntityCategory.CONFIG),
    _desc("humidity_offset", "rh_off", 0.5, PERCENTAGE, integer=False,
          category=EntityCategory.CONFIG),
)


async def async_setup_entry(hass: HomeAssistant, entry: Th5ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(Th5Number(entry.runtime_data, d) for d in NUMBERS)


class Th5Number(Th5Entity, NumberEntity):
    entity_description: Th5NumberDescription

    def __init__(self, runtime: Th5Display, description: Th5NumberDescription) -> None:
        super().__init__(runtime, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        return self.runtime.settings[self.entity_description.setting]

    async def async_set_native_value(self, value: float) -> None:
        d = self.entity_description
        await self.runtime.async_set(d.setting, int(round(value)) if d.integer else round(value, 2))
