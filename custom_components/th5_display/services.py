"""Actions: th5_display.show_image, th5_display.refresh and th5_display.install_firmware.

They only prepare a frame or a firmware package; it reaches the device at its
next sync.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import aiohttp
import probatio
from homeassistant.components import image as image_component
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_source import async_resolve_media
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import MediaSelector, MediaSelectorConfig
from homeassistant.helpers.service import async_register_admin_service

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DITHER,
    ATTR_FIT,
    ATTR_MEDIA,
    ATTR_PATH,
    CONF_DEVICE_ID,
    DOMAIN,
    SERVICE_INSTALL_FIRMWARE,
    SERVICE_REFRESH,
    SERVICE_SHOW_IMAGE,
)
from .firmware import MAX_PACKAGE, path_inside
from .picture import DITHERS, FITS
from .runtime import Th5ConfigEntry

MAX_DOWNLOAD = 20 * 1024 * 1024          # bytes; the source is downscaled to 1600 px anyway

_TARGET = {
    probatio.Optional(ATTR_DEVICE_ID): probatio.All(cv.ensure_list, [cv.string]),
    probatio.Optional(ATTR_CONFIG_ENTRY_ID): probatio.All(cv.ensure_list, [cv.string]),
}

SHOW_IMAGE_SCHEMA = probatio.Schema({
    **_TARGET,
    probatio.Required(ATTR_MEDIA): MediaSelector(MediaSelectorConfig(accept=["image/*"])),
    probatio.Optional(ATTR_FIT, default="contain"): probatio.In(FITS),
    probatio.Optional(ATTR_DITHER, default="floyd_steinberg"): probatio.In(DITHERS),
})

REFRESH_SCHEMA = probatio.Schema(_TARGET)

INSTALL_FIRMWARE_SCHEMA = probatio.Schema({
    **_TARGET,
    probatio.Required(ATTR_PATH): cv.string,
})


def _entries(call: ServiceCall, *, all_if_none: bool) -> list[Th5ConfigEntry]:
    """The loaded entries a call targets: by ESPHome device or config entry id.

    Without a target, all loaded entries if `all_if_none`, else the only one."""
    loaded: list[Th5ConfigEntry] = [
        e for e in call.hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]
    devices = set(call.data.get(ATTR_DEVICE_ID, []))
    entry_ids = set(call.data.get(ATTR_CONFIG_ENTRY_ID, []))
    if devices or entry_ids:
        chosen = [e for e in loaded
                  if e.entry_id in entry_ids or e.data[CONF_DEVICE_ID] in devices]
    elif all_if_none or len(loaded) == 1:
        chosen = loaded
    else:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_target")
    if not chosen:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_display")
    return chosen


async def _async_download(hass: HomeAssistant, url: str) -> bytes:
    """A media URL as bytes; HA's own relative URLs are signed for 5 min."""
    if not url.startswith(("http://", "https://")):
        try:
            url = get_url(hass) + async_sign_path(
                hass, url, dt.timedelta(minutes=5), use_content_user=True)
        except NoURLAvailableError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="media_failed",
                translation_placeholders={"error": str(err)}) from err
    try:
        async with async_get_clientsession(hass).get(url) as resp:
            resp.raise_for_status()
            if (resp.content_length or 0) > MAX_DOWNLOAD:
                raise _too_big()
            data = await resp.read()
    except aiohttp.ClientError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="media_failed",
            translation_placeholders={"error": str(err)}) from err
    if len(data) > MAX_DOWNLOAD:
        raise _too_big()
    return data


def _too_big() -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key="media_failed",
        translation_placeholders={"error": f"larger than {MAX_DOWNLOAD // 2**20} MB"})


def _read_allowed(hass: HomeAssistant, path: Path) -> bytes:
    """A local media file, only from an allowlisted directory (media dirs,
    allowlist_external_dirs); runs in the executor."""
    if not hass.config.is_allowed_path(str(path)):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="path_not_allowed",
            translation_placeholders={"path": str(path)})
    return path.read_bytes()


async def async_media_bytes(hass: HomeAssistant, media: dict[str, Any]) -> bytes:
    """Image bytes for a media selector value.

    Camera and image entities resolve to endless MJPEG streams, so a single
    snapshot is taken from the entity instead (as core's opendisplay does);
    a local media file is read from disk; anything else is downloaded."""
    content_id: str = media["media_content_id"]
    try:
        if content_id.startswith("media-source://camera/"):
            # Imported here: camera pulls in turbojpeg, which only matters when used.
            from homeassistant.components import camera  # noqa: PLC0415

            shot = await camera.async_get_image(hass, content_id.removeprefix("media-source://camera/"))
            return shot.content
        if content_id.startswith("media-source://image/"):
            img = await image_component.async_get_image(
                hass, content_id.removeprefix("media-source://image/"))
            return img.content
        resolved = await async_resolve_media(hass, content_id, None)
    except HomeAssistantError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="media_failed",
            translation_placeholders={"error": str(err)}) from err
    if resolved.path is not None:
        try:
            return await hass.async_add_executor_job(_read_allowed, hass, resolved.path)
        except OSError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="media_failed",
                translation_placeholders={"error": str(err)}) from err
    return await _async_download(hass, resolved.url)


async def _async_show_image(call: ServiceCall) -> None:
    entries = _entries(call, all_if_none=False)
    data = await async_media_bytes(call.hass, call.data[ATTR_MEDIA])
    for entry in entries:
        await entry.runtime_data.async_set_picture(data, call.data[ATTR_FIT], call.data[ATTR_DITHER])


async def _async_refresh(call: ServiceCall) -> None:
    for entry in _entries(call, all_if_none=True):
        await entry.runtime_data.async_refresh_forecast()


def _read_firmware(base: str, path: str) -> bytes:
    """A .fwu file from inside the config directory; runs in the executor."""
    p = path_inside(base, path)
    if p is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="firmware_path_outside",
            translation_placeholders={"path": path})
    if p.stat().st_size > MAX_PACKAGE:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="firmware_invalid",
            translation_placeholders={"error": f"larger than {MAX_PACKAGE} B"})
    return p.read_bytes()


async def _async_install_firmware(call: ServiceCall) -> None:
    """Queue a .fwu from /config (for test images); the bundled one is the update entity's."""
    entries = _entries(call, all_if_none=False)
    hass = call.hass
    try:
        data = await hass.async_add_executor_job(
            _read_firmware, hass.config.config_dir, call.data[ATTR_PATH])
    except OSError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="firmware_unreadable",
            translation_placeholders={"error": str(err)}) from err
    for entry in entries:
        await entry.runtime_data.async_queue_firmware(data)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(DOMAIN, SERVICE_SHOW_IMAGE, _async_show_image,
                                 schema=SHOW_IMAGE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _async_refresh, schema=REFRESH_SCHEMA)
    # Reads files under /config: admin users only.
    async_register_admin_service(hass, DOMAIN, SERVICE_INSTALL_FIRMWARE, _async_install_firmware,
                                 schema=INSTALL_FIRMWARE_SCHEMA)
