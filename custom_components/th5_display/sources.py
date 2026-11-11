"""Adapters from weather data to the normalized forecast model.

- from_open_meteo: Open-Meteo /v1/forecast JSON (primary, models=icon_seamless).
- from_ha_forecast: HA `weather.get_forecasts` (type: hourly) output plus the
  weather entity's state and attributes (fallback, e.g. Met.no).
- from_sample: the mockup JSON (design/mockups/sample_forecast.json).

Internal units are degC and mm. Pure Python, no HA imports.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .forecast import Forecast, HourPoint, Now

# Oslo, design/research-weather.md
OSLO = (59.9139, 10.7522, "Europe/Oslo")

HOURLY_VARS = ("temperature_2m", "apparent_temperature", "precipitation",
               "precipitation_probability", "weather_code", "is_day")
CURRENT_VARS = ("temperature_2m", "apparent_temperature", "precipitation", "weather_code", "is_day")


def open_meteo_url(lat: float = OSLO[0], lon: float = OSLO[1], tz: str = OSLO[2],
                   hours: int = 50, model: str = "icon_seamless") -> str:
    """Request URL. `hours` > 48 so 48 h are left after dropping the past
    part of the current hour and shifting precipitation (see below)."""
    q = {
        "latitude": lat, "longitude": lon, "timezone": tz, "models": model,
        "hourly": ",".join(HOURLY_VARS), "current": ",".join(CURRENT_VARS),
        "daily": "sunrise,sunset", "forecast_hours": hours, "forecast_days": 3,
    }
    return "https://api.open-meteo.com/v1/forecast?" + urlencode(q)


# WMO weather interpretation code -> HA condition (as HA's open_meteo integration).
WMO_TO_HA = {
    0: "sunny", 1: "sunny", 2: "partlycloudy", 3: "cloudy",
    45: "fog", 48: "fog",
    51: "rainy", 53: "rainy", 55: "rainy", 56: "snowy-rainy", 57: "snowy-rainy",
    61: "rainy", 63: "rainy", 65: "pouring", 66: "snowy-rainy", 67: "snowy-rainy",
    71: "snowy", 73: "snowy", 75: "snowy", 77: "snowy",
    80: "rainy", 81: "rainy", 82: "pouring", 85: "snowy", 86: "snowy",
    95: "lightning-rainy", 96: "lightning-rainy", 99: "lightning-rainy",
}


def wmo_condition(code, is_day=None) -> str:
    c = WMO_TO_HA.get(int(code), "cloudy") if code is not None else "cloudy"
    if c == "sunny" and is_day is False:
        c = "clear-night"
    return c


def _om_tz(d: dict) -> tuple[dt.tzinfo, dt.tzinfo]:
    """(offset, zone). Open-Meteo writes every ISO time of a response with
    one fixed offset, utc_offset_seconds, even across a DST change, so times
    are parsed with that offset and then converted to the DST-aware zone."""
    off = dt.timezone(dt.timedelta(seconds=d.get("utc_offset_seconds", 0)))
    try:
        return off, ZoneInfo(d["timezone"])
    except Exception:
        return off, off


def _om_time(v, tz) -> dt.datetime:
    """Open-Meteo time: local ISO without offset, or unixtime."""
    off, zone = tz
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v, zone)
    t = dt.datetime.fromisoformat(v)
    return (t if t.tzinfo else t.replace(tzinfo=off)).astimezone(zone)


def _flag(v):
    return None if v is None else bool(v)


def from_open_meteo(d: dict, location: str = "Oslo") -> Forecast:
    """Open-Meteo JSON -> Forecast.

    Open-Meteo's hourly `precipitation` and `precipitation_probability` at
    time T cover the *preceding* hour, while the chart draws hour i's bar
    from T to T+1. So both are shifted one step earlier here; the last hour
    gets no precipitation."""
    tz = _om_tz(d)
    hr = d["hourly"]
    times = [_om_time(t, tz) for t in hr["time"]]
    n = len(times)
    col = lambda k: (list(hr.get(k) or []) + [None] * n)[:n]  # noqa: E731
    temp, mm, prob = col("temperature_2m"), col("precipitation"), col("precipitation_probability")
    code, day = col("weather_code"), col("is_day")
    hours = []
    for i, t in enumerate(times):
        if temp[i] is None:
            continue
        nmm = mm[i + 1] if i + 1 < n else None
        npr = prob[i + 1] if i + 1 < n else None
        hours.append(HourPoint(
            time=t, temp=float(temp[i]),
            precip_mm=float(nmm or 0.0),
            precip_prob=None if npr is None else float(npr),
            condition=wmo_condition(code[i], _flag(day[i])),
            is_day=_flag(day[i])))

    sun = []
    daily = d.get("daily") or {}
    for r, s in zip(daily.get("sunrise") or [], daily.get("sunset") or []):
        if r and s:
            sun.append((_om_time(r, tz), _om_time(s, tz)))

    cur = d.get("current")
    if cur:
        now = Now(outdoor_temp=cur.get("temperature_2m"), feels_like=cur.get("apparent_temperature"),
                  condition=wmo_condition(cur.get("weather_code"), _flag(cur.get("is_day"))),
                  precip_rate=cur.get("precipitation"), is_day=_flag(cur.get("is_day")))
    elif hours:  # no `current` block: use the first hour kept
        h0 = hours[0]
        app = col("apparent_temperature")[times.index(h0.time)]
        now = Now(h0.temp, app, h0.condition, h0.precip_mm, h0.is_day)
    else:
        now = Now(None, None, "cloudy", None, None)
    return Forecast(location=location, now=now, hourly=hours, sun=sun)


def _to_c(v, unit):
    if v is None:
        return None
    return (float(v) - 32) * 5 / 9 if unit in ("°F", "F") else float(v)


def _to_mm(v, unit):
    if v is None:
        return None
    return float(v) * 25.4 if unit in ("in", "inch") else float(v)


def from_ha_forecast(forecast, attrs: dict, condition: str | None = None,
                     location: str | None = None,
                     sun: list[tuple[dt.datetime, dt.datetime]] | None = None,
                     tz: dt.tzinfo | None = None) -> Forecast:
    """HA `weather.get_forecasts` hourly output -> Forecast.

    forecast: the list of forecast dicts, or the whole service response
      ({"weather.x": {"forecast": [...]}}).
    attrs: the weather entity's attributes (temperature, apparent_temperature,
      temperature_unit, precipitation_unit, friendly_name).
    condition: the entity's state (the current condition).
    sun: optional (sunrise, sunset) pairs, e.g. from sun.sun; without them
      night icons fall back to a fixed 07-17 day.
    Temperatures and precipitation are converted from the entity's units."""
    if isinstance(forecast, dict):
        forecast = next(iter(forecast.values()))["forecast"]
    tu = attrs.get("temperature_unit", "°C")
    pu = attrs.get("precipitation_unit", "mm")
    hours = []
    for f in forecast:
        if f.get("temperature") is None:
            continue
        t = dt.datetime.fromisoformat(f["datetime"])
        if t.tzinfo is None:
            t = t.replace(tzinfo=tz or dt.timezone.utc)
        hours.append(HourPoint(
            time=t, temp=_to_c(f["temperature"], tu),
            precip_mm=_to_mm(f.get("precipitation"), pu) or 0.0,
            precip_prob=f.get("precipitation_probability"),
            condition=f.get("condition") or "cloudy",
            is_day=f.get("is_daytime")))
    hours.sort(key=lambda h: h.time.timestamp())
    cond = condition or attrs.get("condition") or (hours[0].condition if hours else "cloudy")
    now = Now(outdoor_temp=_to_c(attrs.get("temperature"), tu),
              feels_like=_to_c(attrs.get("apparent_temperature"), tu),
              condition=cond,
              precip_rate=hours[0].precip_mm if hours else None)
    return Forecast(location=location or attrs.get("friendly_name", ""), now=now,
                    hourly=hours, sun=list(sun or []))


def from_sample(d: dict) -> Forecast:
    """design/mockups/sample_forecast.json -> Forecast."""
    hours = [HourPoint(dt.datetime.fromisoformat(h["time"]), float(h["temp"]),
                       float(h.get("precip_mm", 0.0)), h.get("precip_prob"),
                       h.get("condition", "cloudy")) for h in d["hourly"]]
    sun = []
    s = d.get("sun") or {}
    if "sunrise" in s and "sunset" in s:
        rise, sset = dt.time.fromisoformat(s["sunrise"]), dt.time.fromisoformat(s["sunset"])
        for day in sorted({h.time.date() for h in hours}):
            tz = hours[0].time.tzinfo
            sun.append((dt.datetime.combine(day, rise, tz), dt.datetime.combine(day, sset, tz)))
    n = d["now"]
    now = Now(n.get("outdoor_temp"), n.get("feels_like"), n.get("condition", "cloudy"),
              n.get("precip_rate"))
    return Forecast(location=d.get("location", ""), now=now, hourly=hours, sun=sun)
