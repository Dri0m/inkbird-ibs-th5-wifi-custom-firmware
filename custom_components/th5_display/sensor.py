"""Sync status sensors: last sync, next sync (estimate), result, frame id."""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import Th5Entity
from .runtime import RESULTS, Th5ConfigEntry, Th5Display

PARALLEL_UPDATES = 0


def _frame_id(rt: Th5Display) -> str | None:
    frame = rt.current_frame()
    return f"{frame.frame_id:016x}" if frame else None


def _result_attrs(rt: Th5Display) -> dict[str, Any]:
    s = rt.status
    return {"error": s.error, "duration": s.duration,
            "device_frame_id": f"{s.hello.frame_id:016x}" if s.hello else None,
            "forecast_problem": rt.forecast_problem}


@dataclass(frozen=True, kw_only=True)
class Th5SensorDescription(SensorEntityDescription):
    value: Callable[[Th5Display], dt.datetime | str | None]
    attrs: Callable[[Th5Display], dict[str, Any]] | None = None


SENSORS = (
    Th5SensorDescription(key="last_sync", translation_key="last_sync",
                         device_class=SensorDeviceClass.TIMESTAMP,
                         value=lambda rt: rt.status.last_sync),
    Th5SensorDescription(key="next_sync", translation_key="next_sync",
                         device_class=SensorDeviceClass.TIMESTAMP,
                         value=lambda rt: rt.next_sync),
    Th5SensorDescription(key="sync_result", translation_key="sync_result",
                         device_class=SensorDeviceClass.ENUM, options=list(RESULTS),
                         entity_category=EntityCategory.DIAGNOSTIC,
                         value=lambda rt: rt.status.result, attrs=_result_attrs),
    Th5SensorDescription(key="frame_id", translation_key="frame_id",
                         entity_category=EntityCategory.DIAGNOSTIC,
                         entity_registry_enabled_default=False, value=_frame_id),
)


async def async_setup_entry(hass: HomeAssistant, entry: Th5ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(Th5Sensor(entry.runtime_data, d) for d in SENSORS)


class Th5Sensor(Th5Entity, SensorEntity):
    entity_description: Th5SensorDescription

    def __init__(self, runtime: Th5Display, description: Th5SensorDescription) -> None:
        super().__init__(runtime, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> dt.datetime | str | None:
        return self.entity_description.value(self.runtime)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs is None:
            return None
        return self.entity_description.attrs(self.runtime)
