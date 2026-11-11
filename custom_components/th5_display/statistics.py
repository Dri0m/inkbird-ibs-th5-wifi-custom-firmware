"""Queue the durable hourly inbox into HA's recorder, idempotently."""
from __future__ import annotations

import datetime as dt

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .history import Hour


@callback
def async_import_hours(hass: HomeAssistant, entry_id: str, name: str, hours: list[Hour]) -> None:
    """Queue both series; raise if recorder cannot accept either one.

    Recorder replaces an existing row with the same (statistic_id, start).
    Queue acceptance is not a database commit, so the caller keeps its inbox
    after success and replays it at startup and on subsequent syncs.
    """
    if not hours:
        return
    for kind, unit, unit_class in (("temperature", UnitOfTemperature.CELSIUS, "temperature"),
                                   ("humidity", "%", None)):
        metadata: StatisticMetaData = {
            "statistic_id": f"{DOMAIN}:{entry_id.lower()}_{kind}",
            "source": DOMAIN, "name": f"{name} {kind.capitalize()}",
            "unit_of_measurement": unit, "unit_class": unit_class,
            "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False,
        }
        rows: list[StatisticData] = [{
            "start": dt.datetime.fromtimestamp(h.start, dt.UTC),
            "mean": (h.t_mean if kind == "temperature" else h.rh_mean) / 100,
            "min": (h.t_min if kind == "temperature" else h.rh_min) / 100,
            "max": (h.t_max if kind == "temperature" else h.rh_max) / 100,
            "mean_weight": h.samples,
        } for h in hours]
        async_add_external_statistics(hass, metadata, rows)
