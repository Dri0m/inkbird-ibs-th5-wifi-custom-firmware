"""Home Assistant fixtures: a fake ESPHome TH5 and a weather entity.

The esphome integration itself is not loaded (aioesphomeapi is not in the
test harness). What th5_display relies on is recreated instead: an esphome
config entry with `device_name`, its device (MAC connection), the Session /
Temperature / Humidity entities with ESPHome-style unique ids, and the three
`esphome.th5_th5_*` actions, which record every call with its loop time.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from homeassistant import loader
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "th5_display"
MAC = "a4:c1:38:12:34:56"
NODE = "th5"
WEATHER = "weather.home"
SESSION = "sensor.inkbird_th5_session"
TEMPERATURE = "sensor.inkbird_th5_temperature"
HUMIDITY = "sensor.inkbird_th5_humidity"
HISTORY = "sensor.inkbird_th5_history"

CUSTOM_COMPONENTS = str(Path(__file__).resolve().parents[2] / "custom_components")



def th5_module(name: str):
    """custom_components.th5_display.<name>; importable once the
    enable_custom_integrations fixture below has run."""
    return importlib.import_module(f"custom_components.th5_display.{name}")


Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class FakeTh5:
    hass: HomeAssistant
    esphome_entry: MockConfigEntry
    device_id: str
    calls: list[tuple[str, dict[str, Any], float]] = field(default_factory=list)
    # action -> coroutine run inside the call (raise to fail, sleep to stall)
    behaviour: dict[str, Handler] = field(default_factory=dict)
    triggered_at: float = 0.0
    n: int = 0

    @property
    def actions(self) -> list[str]:
        return [a for a, _, _ in self.calls]

    def session(self, units: str = "C", frame_id: int = 0, nonce: str | None = None,
                mcu_fw: int | None = None) -> None:
        """Publish a new Session value, as the device does once its API is up."""
        self.n += 1
        nonce = nonce or f"{0xC0FFEE00 + self.n:08x}"
        self.triggered_at = self.hass.loop.time()
        version = f";{mcu_fw:08x};01" if mcu_fw is not None else ""
        self.hass.states.async_set(SESSION, f"{nonce};{units};{frame_id:016x};1a2b3c4d;{self.n:08x}{version}")

    def register_actions(self) -> None:
        for action in ("th5_config", "th5_frame", "th5_done"):
            self.hass.services.async_register(
                "esphome", f"{NODE}_{action}", self._handler(action),
                supports_response=SupportsResponse.NONE)

    def _handler(self, action: str) -> Callable[[ServiceCall], Awaitable[None]]:
        async def handle(call: ServiceCall) -> None:
            self.calls.append((action, dict(call.data), self.hass.loop.time()))
            if (b := self.behaviour.get(action)) is not None:
                await b(action, dict(call.data))
        return handle


def fail_with(message: str) -> Handler:
    async def run(action: str, data: dict[str, Any]) -> None:
        raise HomeAssistantError(f"Failed to perform {action} on th5: {message}")
    return run


def stall(seconds: float) -> Handler:
    async def run(action: str, data: dict[str, Any]) -> None:
        await asyncio.sleep(seconds)
    return run


def hourly_forecast(hours: int = 50) -> list[dict[str, Any]]:
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    return [{
        "datetime": (start + dt.timedelta(hours=i)).isoformat(),
        "temperature": 8 + 6 * ((i % 24) / 12 if i % 24 < 12 else (24 - i % 24) / 12),
        "precipitation": 0.4 if 10 <= i < 14 else 0.0,
        "condition": "rainy" if 10 <= i < 14 else "partlycloudy",
    } for i in range(hours)]


@pytest.fixture
def enable_custom_integrations(hass: HomeAssistant) -> None:
    """The harness's fixture, plus the repo's custom_components on the search path.

    The harness mounts its own testing_config/custom_components package when
    `hass` starts; ours is added to that package's __path__."""
    import custom_components  # noqa: PLC0415

    if CUSTOM_COMPONENTS not in custom_components.__path__:
        custom_components.__path__.append(CUSTOM_COMPONENTS)
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS)


@pytest.fixture
def weather(hass: HomeAssistant) -> list[ServiceCall]:
    """weather.home with an hourly forecast; returns the get_forecasts calls."""
    calls: list[ServiceCall] = []
    hass.states.async_set(WEATHER, "partlycloudy", {
        "supported_features": 3, "temperature": 11.5, "apparent_temperature": 9.0,
        "temperature_unit": UnitOfTemperature.CELSIUS, "precipitation_unit": "mm",
        "friendly_name": "Home"})

    async def get_forecasts(call: ServiceCall) -> dict[str, Any]:
        calls.append(call)
        ids = call.data["entity_id"]
        return {eid: {"forecast": hourly_forecast()} for eid in ([ids] if isinstance(ids, str) else ids)}

    hass.services.async_register("weather", "get_forecasts", get_forecasts,
                                 supports_response=SupportsResponse.ONLY)
    return calls


@pytest.fixture
def th5(hass: HomeAssistant) -> FakeTh5:
    """The ESPHome side of a TH5, as HA's registries and services see it."""
    esp = MockConfigEntry(domain="esphome", title="Inkbird TH5", unique_id=MAC.upper(),
                          data={"host": "192.0.2.30", "port": 6053, "device_name": NODE})
    esp.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=esp.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        name="Inkbird TH5", manufacturer="Espressif", model="cbu")
    ent_reg = er.async_get(hass)
    for esp_type, name, entity_id in (("text_sensor", "Session", SESSION),
                                      ("sensor", "Temperature", TEMPERATURE),
                                      ("sensor", "Humidity", HUMIDITY)):
        ent_reg.async_get_or_create(
            "sensor", "esphome", f"{MAC.upper()}/0/{esp_type}/{name}",
            config_entry=esp, device_id=dev.id, original_name=name,
            suggested_object_id=entity_id.split(".")[1])
    hass.states.async_set(SESSION, "00000000;C;0000000000000000;00000000;00000000")
    hass.states.async_set(TEMPERATURE, "21.4", {"unit_of_measurement": UnitOfTemperature.CELSIUS})
    hass.states.async_set(HUMIDITY, "46", {"unit_of_measurement": "%"})
    fake = FakeTh5(hass, esp, dev.id)
    fake.register_actions()
    return fake


@pytest.fixture
async def entry(hass: HomeAssistant, th5: FakeTh5, weather: list[ServiceCall],
                enable_custom_integrations: None) -> MockConfigEntry:
    """A loaded th5_display entry with both forecast frames rendered."""
    e = MockConfigEntry(domain=DOMAIN, title="Inkbird TH5", unique_id=MAC,
                        data={"device_id": th5.device_id, "weather_entity": WEATHER,
                              "location": "Oslo"})
    e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return e
