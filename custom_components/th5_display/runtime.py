"""Runtime of one TH5 Display entry: settings, frames, forecast and the sync.

The device is asleep almost all the time, so nothing is pushed when a
setting changes: everything is stored here and handed over at the next
session. Frames are rendered ahead of time (forecast in both units, the
picture once) because a session leaves HA 3 s per call; the session handler
only picks a ready slot and sends it.

Persistence: a Store `th5_display.<entry_id>` holds the settings, the last
sync's outcome, the picture options and the firmware queue; the slots, the
downscaled picture source, the queued package and the hourly history inbox
are plain files in `.storage/th5_display/<entry_id>/`. Frame ids come from
the payload hash, so a re-rendered picture keeps its id and is not sent again.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import probatio
from homeassistant.components import persistent_notification
from homeassistant.components.weather import (
    DOMAIN as WEATHER_DOMAIN,
    SERVICE_GET_FORECASTS,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_CORE_CONFIG_UPDATE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
    UnitOfTemperature,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import STORAGE_DIR, Store
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util
from PIL import Image

from . import forecast, history, picture, preview, sources
from .const import (
    CALL_TIMEOUT,
    CONF_LOCATION,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    DONE_GRACE,
    ESPHOME_DOMAIN,
    FORECAST_INTERVAL,
    HOLD,
    LOGGER,
    MODE_FORECAST,
    MODE_PICTURE,
    SESSION_BUDGET,
    STORAGE_VERSION,
)
from .device import DeviceLink, LinkError, async_resolve_link
from .firmware import FirmwareError, Fwu, load_bundled, parse_fwu, version_str
from .frame import FRAME_FLAG_BAND, build_slot, encode_planes, frame_id_of, parse_slot
from .session import DEFAULTS, Call, CallError, Hello, SyncResult, build_config, parse_session, run_sync

BPP = 2
UNITS = ("C", "F")

RESULT_OK = "ok"
RESULT_FRAME_SENT = "frame_sent"
RESULT_FIRMWARE_SENT = "firmware_sent"
RESULT_FAILED = "failed"
RESULTS = (RESULT_OK, RESULT_FRAME_SENT, RESULT_FIRMWARE_SENT, RESULT_FAILED)

FW_FILE = "firmware.fwu"                 # the queued package, next to the frames
HISTORY_FILE = "history.json"            # hourly rows, saved before the MCU may drop them
FW_MAX_ATTEMPTS = 3                      # pushes of one package before the queue is failed


class ForecastError(Exception):
    """The weather entity gave no usable hourly forecast."""


@dataclass(frozen=True, slots=True)
class Frame:
    """A slot ready to upload: header (commit word erased) + payload."""

    slot: bytes
    frame_id: int

    @classmethod
    def from_image(cls, img: Image.Image, flags: int) -> Frame:
        payload = encode_planes(img, BPP)
        fid = frame_id_of(payload, BPP, flags)
        return cls(build_slot(payload, BPP, flags=flags, frame_id=fid, commit=False), fid)

    @classmethod
    def from_slot(cls, data: bytes) -> Frame:
        hdr, _ = parse_slot(data, require_commit=False)
        return cls(data, hdr.frame_id)


@dataclass(slots=True)
class SyncStatus:
    """The last session as HA saw it."""

    last_sync: dt.datetime | None = None
    result: str | None = None            # one of RESULTS
    error: str | None = None
    duration: float | None = None        # s, trigger to th5_done answered
    hello: Hello | None = None
    session_value: str | None = None     # raw sensor value, for diagnostics
    sync_min: int | None = None          # the interval sent in that session
    held_until: dt.datetime | None = None  # maintenance HOLD ends

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "result": self.result,
            "error": self.error,
            "duration": self.duration,
            "session": self.session_value,
            "sync_min": self.sync_min,
            "held_until": self.held_until.isoformat() if self.held_until else None,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> SyncStatus:
        def ts(v: str | None) -> dt.datetime | None:
            return dt_util.parse_datetime(v) if v else None

        return cls(
            last_sync=ts(d.get("last_sync")), result=d.get("result"), error=d.get("error"),
            duration=d.get("duration"), hello=parse_session(d.get("session")),
            session_value=d.get("session"), sync_min=d.get("sync_min"),
            held_until=ts(d.get("held_until")))


# --------------------------------------------------------------------------- executor jobs


def _render_forecast(fc: forecast.Forecast, now: dt.datetime,
                     tz: ZoneInfo) -> tuple[dict[str, Frame], str | None]:
    """Both units' forecast frames (band layout) and the renderer's problem text."""
    frames: dict[str, Frame] = {}
    problem = None
    for u in UNITS:
        img, unit_problem = forecast.render_checked(fc, u, now, tz=tz)
        problem = problem or unit_problem
        frames[u] = Frame.from_image(img, FRAME_FLAG_BAND)
    return frames, problem


# Picture frames per layout (setting "top_bar"): full screen, or under the
# MCU's live band (FRAME_FLAG_BAND, sent with MCU MODE forecast).
PICTURE_FILES = {False: "picture.bin", True: "picture-top.bin"}  # top: 0.2.7 layout with the rule


def _picture_frame(src: Image.Image, fit: str, dither: str, top_bar: bool) -> Frame:
    return Frame.from_image(picture.render(src, fit, dither, top_bar),
                            FRAME_FLAG_BAND if top_bar else 0)


def _prepare_picture(data: bytes, fit: str, dither: str) -> tuple[bytes, dict[bool, Frame]]:
    """Image bytes -> (downscaled source PNG, the picture frame of each layout)."""
    src = picture.shrink(picture.load(data))
    return picture.to_png(src), {t: _picture_frame(src, fit, dither, t) for t in PICTURE_FILES}


def _rerender_picture(source_png: bytes, fit: str, dither: str, top_bar: bool) -> Frame:
    return _picture_frame(picture.load(source_png), fit, dither, top_bar)


def _write_atomic(path: Path, data: bytes, *, durable: bool = False) -> None:
    """Write via a temporary file; `durable` also flushes file and directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        if durable:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)
    if durable:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_history_inbox(path: Path, data: bytes) -> None:
    """The inbox must be on disk before the MCU is told it may drop its hours."""
    _write_atomic(path, data, durable=True)


def _read_files(folder: Path) -> dict[str, bytes]:
    if not folder.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in folder.iterdir()
            if p.is_file() and p.suffix != ".tmp"}


# --------------------------------------------------------------------------- runtime


class Th5Display:
    """Everything one config entry owns; entities read and write through it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, link: DeviceLink,
                 tz: ZoneInfo) -> None:
        self.hass = hass
        self.entry = entry
        self.link = link
        self.tz = tz                         # HA's zone; the renderer needs a ZoneInfo
        self.settings: dict[str, Any] = dict(DEFAULTS)
        self.status = SyncStatus()
        self.forecast_frames: dict[str, Frame] = {}
        self.forecast_updated: dt.datetime | None = None
        self.forecast_problem: str | None = None
        self.picture_frames: dict[bool, Frame] = {}   # by top_bar
        self.picture_options: dict[str, str] | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._dir = Path(hass.config.path(STORAGE_DIR, DOMAIN, entry.entry_id))
        self._listeners: list[CALLBACK_TYPE] = []
        self._session_task: asyncio.Task[None] | None = None
        self._render_lock = asyncio.Lock()
        self._link_lock = asyncio.Lock()     # one conversation with the device at a time
        self._unsub_hold: CALLBACK_TYPE | None = None
        # MCU firmware (update.py): the package shipped with the integration, and
        # the one queued for the next session (None = nothing queued).
        self.fw_latest: Fwu | None = None
        self.fw_queued: Fwu | None = None
        self.fw_attempts = 0                 # sessions that pushed the queued package
        self.history_inbox: list[history.Hour] = []
        self.history_problem: str | None = None

    # ------------------------------------------------------------------ lifecycle

    async def async_load(self) -> None:
        """Settings and status from the Store, frames and the rest from their files."""
        data = await self._store.async_load() or {}
        # Unknown keys are dropped, e.g. "badge" (<= 0.2.5), replaced by "top_bar".
        stored = data.get("settings") or {}
        self.settings.update({k: v for k, v in stored.items() if k in DEFAULTS})
        if stored.get("badge") and "top_bar" not in stored:
            self.settings["top_bar"] = True   # <= 0.2.5: the badge's job is now the top bar
        self.status = SyncStatus.from_dict(data.get("status") or {})
        self.picture_options = data.get("picture")
        files = await self.hass.async_add_executor_job(_read_files, self._dir)

        if (raw := files.get(HISTORY_FILE)) is not None:
            try:
                self.history_inbox = history.load_inbox(json.loads(raw))
            except (ValueError, TypeError) as err:
                LOGGER.warning("%s: ignoring corrupt history inbox: %s", self.link.name, err)
        self._import_history(self.history_inbox)   # replay: queueing is not a commit

        try:
            self.fw_latest = await self.hass.async_add_executor_job(load_bundled)
        except (FirmwareError, OSError) as err:
            LOGGER.warning("%s: the bundled MCU firmware package is unusable: %s",
                           self.link.name, err)
        queued = data.get("firmware")
        if queued and (raw := files.get(FW_FILE)) is not None:
            try:
                self.fw_queued = parse_fwu(raw)
            except FirmwareError as err:
                LOGGER.warning("%s: dropping the stored MCU firmware queue: %s", self.link.name, err)
            else:
                self.fw_attempts = int(queued.get("attempts", 0))

        for u in UNITS:
            if (raw := files.get(f"frame-{u}.bin")) is not None:
                with contextlib.suppress(ValueError):   # rendered again at startup
                    self.forecast_frames[u] = Frame.from_slot(raw)
        if self.picture_options is not None:
            for top_bar, name in PICTURE_FILES.items():
                if (raw := files.get(name)) is not None:
                    with contextlib.suppress(ValueError):   # re-rendered from the source below
                        self.picture_frames[top_bar] = Frame.from_slot(raw)
                if top_bar not in self.picture_frames and (src := files.get("source.png")) is not None:
                    # A store from <= 0.2.5 has no top-bar frame yet.
                    try:
                        frame = await self.hass.async_add_executor_job(
                            _rerender_picture, src, self.picture_options["fit"],
                            self.picture_options["dither"], top_bar)
                        await self.hass.async_add_executor_job(_write_atomic, self._dir / name, frame.slot)
                    except (OSError, ValueError) as err:
                        LOGGER.warning("%s: the stored picture could not be re-rendered: %s",
                                       self.link.name, err)
                    else:
                        self.picture_frames[top_bar] = frame

    @callback
    def async_start(self) -> None:
        """Listen for sessions, refresh the forecast now and every 30 min."""
        self.entry.async_on_unload(async_track_state_change_event(
            self.hass, [self.link.session_entity_id], self._async_session_changed))
        linked = [e for e in (self.link.session_entity_id, self.link.temperature_entity_id,
                              self.link.humidity_entity_id, self.link.history_entity_id) if e]
        self.entry.async_on_unload(async_track_entity_registry_updated_event(
            self.hass, linked, self._async_link_entity_changed))
        self.entry.async_on_unload(self.hass.bus.async_listen(
            EVENT_CORE_CONFIG_UPDATE, self._async_core_config_updated))
        self.entry.async_on_unload(async_track_time_interval(
            self.hass, self._async_scheduled_refresh, FORECAST_INTERVAL,
            name=f"{DOMAIN} forecast refresh"))
        self.entry.async_on_unload(async_at_started(self.hass, self._async_scheduled_refresh))
        self._arm_hold_timer()
        self.entry.async_on_unload(self._cancel_hold_timer)

    async def async_stop(self) -> None:
        if self._session_task is not None:
            self._session_task.cancel()
        await self._store.async_save(self._data_to_save())

    @callback
    def _async_link_entity_changed(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """An ESPHome entity the link uses was renamed, disabled or removed:
        reload, or the Session trigger would listen to a stale entity id."""
        data = event.data
        if data["action"] != "update" or "old_entity_id" in data or "disabled_by" in data["changes"]:
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    @callback
    def _async_core_config_updated(self, _event: Event) -> None:
        """HA's time zone changed: reload, so TIME and the frames use the new zone."""
        if self.hass.config.time_zone != str(self.tz):
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    # ------------------------------------------------------------------ listeners

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    def _data_to_save(self) -> dict[str, Any]:
        return {"settings": self.settings, "status": self.status.as_dict(),
                "picture": self.picture_options,
                "firmware": ({"version": self.fw_queued.version, "attempts": self.fw_attempts}
                             if self.fw_queued else None)}

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 1)

    @callback
    def _changed(self) -> None:
        self._save()
        self._notify()

    # ------------------------------------------------------------------ state for entities

    @property
    def picture_frame(self) -> Frame | None:
        """The picture frame of the chosen layout (setting "top_bar")."""
        return self.picture_frames.get(bool(self.settings["top_bar"]))

    @property
    def mode(self) -> str:
        """The mode sent to the device: picture only once there is a picture."""
        if self.settings["mode"] == MODE_PICTURE and self.picture_frame is not None:
            return MODE_PICTURE
        return MODE_FORECAST

    @property
    def units(self) -> str:
        """The device's display units, as last reported; C before any sync."""
        return self.status.hello.units if self.status.hello else "C"

    def current_frame(self, units: str | None = None) -> Frame | None:
        """The frame the device should hold for `units` (default: its units)."""
        if self.mode == MODE_PICTURE:
            return self.picture_frame
        return self.forecast_frames.get(units or self.units)

    @property
    def display_up_to_date(self) -> bool | None:
        """Whether the device's committed frame (last session) is our current one."""
        frame = self.current_frame()
        if self.status.hello is None or frame is None:
            return None
        return self.status.hello.frame_id == frame.frame_id

    @property
    def next_sync(self) -> dt.datetime | None:
        """Estimate: last sync + the interval sent then. The MCU aligns syncs to
        its clock and backs off after failures, so this is approximate."""
        if self.status.last_sync is None or self.status.sync_min is None:
            return None
        return self.status.last_sync + dt.timedelta(minutes=self.status.sync_min)

    @property
    def held(self) -> bool:
        """The device is (probably) held awake by maintenance right now."""
        return self.status.held_until is not None and dt_util.utcnow() < self.status.held_until

    @property
    def mcu_fw(self) -> int | None:
        """The MCU app's fw_version from the last session."""
        return self.status.hello.mcu_fw if self.status.hello else None

    @property
    def boot_ver(self) -> int | None:
        """The bootloader version from the last session (0 = none installed)."""
        return self.status.hello.boot_ver if self.status.hello else None

    # ------------------------------------------------------------------ MCU firmware

    async def async_queue_firmware(self, data: bytes) -> Fwu:
        """Queue a .fwu package for the next session.

        The queue is the package file `firmware.fwu` in the entry's folder plus
        {version, attempts} in the Store, so it survives a restart. It ends
        when a session reports the package's version, when a push is refused,
        or after FW_MAX_ATTEMPTS pushes that left another version running."""
        try:
            fwu = parse_fwu(data)
        except FirmwareError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="firmware_invalid",
                translation_placeholders={"error": str(err)}) from err
        if self.boot_ver == 0:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_bootloader")
        if self.boot_ver is not None and fwu.min_boot_ver > self.boot_ver:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="bootloader_too_old",
                translation_placeholders={"need": str(fwu.min_boot_ver), "have": str(self.boot_ver)})
        if not self.hass.services.has_service(ESPHOME_DOMAIN, self.link.action("th5_firmware")):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_firmware_action")
        await self.hass.async_add_executor_job(_write_atomic, self._dir / FW_FILE, data)
        self.fw_queued, self.fw_attempts = fwu, 0
        self._dismiss_fw_notification()
        self._changed()
        return fwu

    def _plan_firmware(self, hello: Hello) -> Fwu | None:
        """The package to push in the session `hello` opens, or None.

        Settles the queue first: done if the device runs the queued version,
        failed after FW_MAX_ATTEMPTS pushes or without a bootloader (or with
        an old CBU that does not report one)."""
        fwu = self.fw_queued
        if fwu is None:
            return None
        if hello.mcu_fw == fwu.version:
            LOGGER.info("%s: MCU firmware %s is installed", self.link.name, fwu.version_str)
            self._clear_firmware()
            return None
        if self.fw_attempts >= FW_MAX_ATTEMPTS:
            running = "no version" if hello.mcu_fw is None else version_str(hello.mcu_fw)
            self._fail_firmware(f"the device still reports {running} after "
                                f"{self.fw_attempts} pushes; the MCU did not install {fwu.version_str}")
            return None
        if not hello.boot_ver:
            self._fail_firmware("the device reports no bootloader (boot_ver 0), or its CBU "
                                "firmware is too old to report one")
            return None
        return fwu

    @callback
    def _clear_firmware(self) -> None:
        self.fw_queued, self.fw_attempts = None, 0
        self.hass.async_add_executor_job(self._remove_fw_file)
        self._dismiss_fw_notification()
        self._save()

    def _remove_fw_file(self) -> None:
        if self.fw_queued is None:           # else a new package was queued meanwhile
            (self._dir / FW_FILE).unlink(missing_ok=True)

    @callback
    def _fail_firmware(self, reason: str) -> None:
        version = self.fw_queued.version_str if self.fw_queued else "?"
        LOGGER.warning("%s: MCU firmware %s was not installed: %s", self.link.name, version, reason)
        self._clear_firmware()
        persistent_notification.async_create(
            self.hass, f"The MCU firmware {version} was not installed: {reason}.",
            title=f"{self.link.name}: MCU firmware update failed",
            notification_id=f"{DOMAIN}_{self.entry.entry_id}_firmware")

    @callback
    def _dismiss_fw_notification(self) -> None:
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{self.entry.entry_id}_firmware")

    # ------------------------------------------------------------------ settings

    async def async_set(self, key: str, value: Any) -> None:
        """Change a setting; it reaches the device at the next sync."""
        if key == "mode" and value == MODE_PICTURE and self.picture_frame is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_picture")
        if self.settings[key] == value:
            return
        self.settings[key] = value
        self._changed()
        if key == "maintenance" and not value and self.held:
            # The device is still up under HOLD: end it now rather than at 15 min.
            self.entry.async_create_background_task(
                self.hass, self._async_end_hold(), f"{DOMAIN} end maintenance")

    # ------------------------------------------------------------------ forecast

    async def _async_scheduled_refresh(self, *_: Any) -> None:
        with contextlib.suppress(HomeAssistantError):   # logged; the old frames stay in use
            await self.async_refresh_forecast()

    def _set_forecast_problem(self, problem: str | None) -> None:
        if problem and problem != self.forecast_problem:
            LOGGER.warning("%s: forecast: %s", self.link.name, problem)
        self.forecast_problem = problem

    async def async_refresh_forecast(self) -> None:
        """Fetch the hourly forecast and render both units (raises HomeAssistantError)."""
        async with self._render_lock:
            try:
                fc = await self._async_fetch_forecast()
            except ForecastError as err:
                self._set_forecast_problem(f"not updated: {err}")
                self._notify()
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="forecast_failed",
                    translation_placeholders={"error": str(err)}) from err
            frames, problem = await self.hass.async_add_executor_job(
                _render_forecast, fc, dt_util.now(self.tz), self.tz)
            for u, f in frames.items():
                if self.forecast_frames.get(u) != f:
                    await self.hass.async_add_executor_job(
                        _write_atomic, self._dir / f"frame-{u}.bin", f.slot)
            self.forecast_frames = frames
            self.forecast_updated = dt_util.utcnow()
            self._set_forecast_problem(problem)
        self._notify()

    async def _async_fetch_forecast(self) -> forecast.Forecast:
        entity_id: str = self.entry.data[CONF_WEATHER_ENTITY]
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            raise ForecastError(f"{entity_id} is unavailable")
        try:
            resp = await self.hass.services.async_call(
                WEATHER_DOMAIN, SERVICE_GET_FORECASTS, {"type": "hourly"},
                target={"entity_id": entity_id}, blocking=True, return_response=True)
        except (HomeAssistantError, probatio.Invalid) as err:
            raise ForecastError(f"{entity_id}: {err}") from err
        hourly = (resp or {}).get(entity_id, {}).get("forecast")
        if not hourly:
            raise ForecastError(f"{entity_id} returned no hourly forecast")
        location = self.entry.data.get(CONF_LOCATION) or self.hass.config.location_name
        try:
            return sources.from_ha_forecast(hourly, dict(state.attributes), state.state,
                                            location, self._sun_pairs(), self.tz)
        except (KeyError, TypeError, ValueError) as err:
            raise ForecastError(f"{entity_id}: unreadable forecast: {err!r}") from err

    def _sun_pairs(self) -> list[tuple[dt.datetime, dt.datetime]]:
        """(sunrise, sunset) for today and the next two days, from HA's location."""
        today = dt_util.now(self.tz).date()
        out = []
        for d in range(3):
            day = today + dt.timedelta(days=d)
            rise = get_astral_event_date(self.hass, SUN_EVENT_SUNRISE, day)
            sset = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, day)
            if rise is not None and sset is not None:   # None: polar day or night
                out.append((rise.astimezone(self.tz), sset.astimezone(self.tz)))
        return out

    # ------------------------------------------------------------------ picture

    async def async_set_picture(self, data: bytes, fit: str, dither: str) -> None:
        """New picture: keep a downscaled source, render the frame, switch to picture."""
        async with self._render_lock:
            try:
                source_png, frames = await self.hass.async_add_executor_job(
                    _prepare_picture, data, fit, dither)
            except (OSError, ValueError, Image.DecompressionBombError) as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="bad_image",
                    translation_placeholders={"error": str(err)}) from err
            await self.hass.async_add_executor_job(_write_atomic, self._dir / "source.png", source_png)
            for top_bar, name in PICTURE_FILES.items():
                await self.hass.async_add_executor_job(
                    _write_atomic, self._dir / name, frames[top_bar].slot)
            self.picture_frames = frames
            self.picture_options = {"fit": fit, "dither": dither}
            self.settings["mode"] = MODE_PICTURE
        self._changed()

    # ------------------------------------------------------------------ preview

    def _indoor(self, entity_id: str | None, units: str | None = None) -> float | None:
        """A numeric ESPHome reading; temperatures converted to `units`."""
        if entity_id is None or (state := self.hass.states.get(entity_id)) is None:
            return None
        try:
            v = float(state.state)
        except ValueError:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if units == "F" and unit == UnitOfTemperature.CELSIUS:
            v = v * 9 / 5 + 32
        elif units == "C" and unit == UnitOfTemperature.FAHRENHEIT:
            v = (v - 32) * 5 / 9
        return v

    def preview_band(self) -> preview.Band:
        return preview.Band(
            now=dt_util.now(self.tz), clock_24h=bool(self.settings["clock_24h"]),
            units=self.units,
            temp=self._indoor(self.link.temperature_entity_id, self.units),
            humidity=self._indoor(self.link.humidity_entity_id),
            monochrome=bool(self.settings["monochrome"]))

    async def async_preview_png(self) -> bytes | None:
        if (frame := self.current_frame()) is None:
            return None
        return await self.hass.async_add_executor_job(
            preview.render_png, frame.slot, self.preview_band())

    # ------------------------------------------------------------------ hourly history

    def _set_history_problem(self, problem: str | None) -> None:
        if problem and problem != self.history_problem:
            LOGGER.warning("%s: hourly history: %s", self.link.name, problem)
        self.history_problem = problem

    def _import_history(self, hours: list[history.Hour]) -> bool:
        """Queue `hours` for the recorder. Queueing is not a durable commit: the
        inbox on disk is replayed at the next start, and imports are idempotent."""
        if not hours:
            return True
        try:
            # Lazy: the recorder is optional, and without it a sync still works.
            from .statistics import async_import_hours  # noqa: PLC0415

            async_import_hours(self.hass, self.entry.entry_id, self.link.name, hours)
        except (ImportError, KeyError, RuntimeError, HomeAssistantError, ValueError) as err:
            self._set_history_problem(f"recorder did not accept it: {err}")
            return False
        self._set_history_problem(None)
        return True

    async def _async_accept_history(self, value: str, hello: Hello) -> bool:
        """Persist first, then queue; only then may the MCU discard its hours.
        Returns whether to acknowledge them."""
        try:
            received = history.parse_snapshot(value, int(time.time()), nonce=hello.nonce, boot=hello.boot)
        except ValueError as err:
            self._set_history_problem(f"ignored: {err}")
            return False
        if not received:
            return False
        inbox = history.merge_inbox(self.history_inbox, received)
        if inbox == self.history_inbox:
            return True  # a repeat after a lost ACK: already saved and queued
        try:
            # Not Store.async_save: it logs and swallows write errors, and an
            # unsaved inbox must not be acknowledged.
            await self.hass.async_add_executor_job(
                _write_history_inbox, self._dir / HISTORY_FILE,
                json.dumps([h.as_list() for h in inbox], separators=(",", ":")).encode())
        except OSError as err:
            self._set_history_problem(f"could not be saved: {err}")
            return False
        self.history_inbox = inbox
        return self._import_history(received)

    def _history_value(self) -> str | None:
        """History as Session arrives (the CBU publishes it first), or None."""
        if self.link.history_entity_id is None or self.hass.states.get(self.link.history_entity_id) is None:
            # The entity can appear after a CBU upgrade while this entry is loaded.
            with contextlib.suppress(LinkError):
                self.link = async_resolve_link(self.hass, self.link.device_id)
        state = self.hass.states.get(self.link.history_entity_id or "")
        if state is None or state.state in ("", STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        return state.state

    # ------------------------------------------------------------------ session

    @callback
    def _async_session_changed(self, event: Event[EventStateChangedData]) -> None:
        """The device's Session sensor changed: run the sync at once."""
        new, old = event.data["new_state"], event.data["old_state"]
        if new is None or new.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        if old is None or old.state == new.state:
            return                           # entity (re)added with its stored value, or attributes only
        hello = parse_session(new.state)
        if hello is None:
            LOGGER.warning("%s: unreadable session value %r", self.link.name, new.state)
            return
        last = self.status.hello
        if (last is not None and (hello.nonce, hello.boot) == (last.nonce, last.boot)
                and self.status.result != RESULT_FAILED):
            # The session already handled, resent after an API reconnect. The
            # boot counter is part of the key because random_uint32() can repeat
            # across cold boots; a failed session is run again (the reconnect
            # gives HA a fresh 3 s window).
            return
        if self._session_task is not None:
            self._session_task.cancel()      # the device restarted mid-session
        self._session_task = self.entry.async_create_background_task(
            self.hass, self._async_run_session(hello, new.state, self.hass.loop.time(),
                                             self._history_value()),
            f"{DOMAIN} sync {self.link.name}")

    def _caller(self, deadline: float) -> Call:
        """The `call` for session.run_sync: one blocking esphome.* action each,
        with the session's remaining time as the limit."""
        loop = self.hass.loop

        async def call(action: str, data: dict[str, Any]) -> None:
            budget = deadline - loop.time()
            if action == "th5_done":
                budget = max(budget, DONE_GRACE)
            if budget <= 0:
                raise CallError(f"{action}: the session's time is up")
            limit = min(CALL_TIMEOUT, budget)
            try:
                async with asyncio.timeout(limit):
                    # blocking waits for the device's success or error
                    await self.hass.services.async_call(
                        ESPHOME_DOMAIN, self.link.action(action), data, blocking=True)
            except TimeoutError as err:
                raise CallError(f"{action}: no answer within {limit:.1f} s") from err
            except (HomeAssistantError, probatio.Invalid) as err:
                raise CallError(f"{action}: {err}") from err

        return call

    async def _async_run_session(self, hello: Hello, value: str, started: float,
                                 history_value: str | None) -> None:
        started_utc = dt_util.utcnow()
        async with self._link_lock:
            frame = self.current_frame(hello.units)
            settings = {**self.settings, "mode": self.mode}
            config = build_config(settings, self.tz, int(time.time()), mcu_fw=hello.mcu_fw)
            if (history_value is not None and (hello.mcu_fw or 0) >= history.HISTORY_FW
                    and await self._async_accept_history(history_value, hello)):
                config["history_ack"] = True
            fwu = self._plan_firmware(hello)
            result = await run_sync(self._caller(started + SESSION_BUDGET), hello, config,
                                    frame.slot if frame else None,
                                    frame.frame_id if frame else 0,
                                    fwu.data if fwu else None)
        if fwu is not None:
            self._settle_push(result, fwu)
        self._record(hello, value, settings, result, started_utc=started_utc,
                     duration=self.hass.loop.time() - started)

    @callback
    def _settle_push(self, result: SyncResult, fwu: Fwu) -> None:
        """After a session that offered `fwu`: a refused th5_firmware ends the
        queue; every chunk accepted counts as one push (the MCU's reset and the
        new version show in the next session). The CBU can still skip or fail
        the push to the MCU after th5_done, so up to FW_MAX_ATTEMPTS pushes are
        tried. Nothing is counted if another package was queued meanwhile."""
        if self.fw_queued is not fwu:
            return
        if result.failed_action == "th5_firmware":
            self._fail_firmware(f"the device refused it ({result.error})")
        elif result.firmware_sent:
            self.fw_attempts += 1

    @callback
    def _record(self, hello: Hello, value: str, settings: Mapping[str, Any],
                result: SyncResult, *, started_utc: dt.datetime, duration: float) -> None:
        # th5_config carries the maintenance flag; once it is in, the MCU holds.
        held = settings["maintenance"] and (result.ok or result.failed_action != "th5_config")
        self.status = SyncStatus(
            last_sync=dt_util.utcnow(),
            result=(RESULT_FAILED if not result.ok
                    else RESULT_FRAME_SENT if result.frame_sent
                    else RESULT_FIRMWARE_SENT if result.firmware_sent else RESULT_OK),
            error=result.error, duration=round(duration, 2), hello=hello,
            session_value=value, sync_min=int(settings["sync_min"]),
            held_until=started_utc + HOLD if held else None)
        if not result.ok:
            LOGGER.warning("%s: sync failed after %.1f s: %s", self.link.name, duration, result.error)
        self._arm_hold_timer()
        self._changed()

    # ------------------------------------------------------------------ maintenance

    @callback
    def _cancel_hold_timer(self) -> None:
        if self._unsub_hold is not None:
            self._unsub_hold()
            self._unsub_hold = None

    @callback
    def _arm_hold_timer(self) -> None:
        """At the end of the HOLD, clear maintenance (it lasts one session)."""
        self._cancel_hold_timer()
        if self.status.held_until is not None:
            self._unsub_hold = async_track_point_in_utc_time(
                self.hass, self._async_hold_over, self.status.held_until)

    @callback
    def _async_hold_over(self, _now: dt.datetime) -> None:
        self._unsub_hold = None
        self.status.held_until = None
        self.settings["maintenance"] = False
        self._changed()

    async def _async_end_hold(self) -> None:
        """Maintenance switched off while the device is held: config without
        maintenance, then th5_done ends the session (docs/integration-contract.md).
        If that fails, the HOLD runs out on the device by itself."""
        if (hello := self.status.hello) is None:
            return
        async with self._link_lock:
            config = build_config({**self.settings, "mode": self.mode, "maintenance": False},
                                  self.tz, int(time.time()), mcu_fw=hello.mcu_fw)
            await run_sync(self._caller(self.hass.loop.time() + SESSION_BUDGET),
                           hello, config, None, 0)
        self.status.held_until = None
        self._cancel_hold_timer()
        self._changed()


type Th5ConfigEntry = ConfigEntry[Th5Display]
