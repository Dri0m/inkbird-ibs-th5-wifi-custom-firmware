"""MCU firmware: the app on the HC32, updated over the link (design 4.7).

Installing only queues the bundled package; it is pushed at the next session.
The entity stays in progress until a session reports the new version, or the
queue is dropped (runtime.py _plan_firmware).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import Th5Entity
from .firmware import version_str
from .runtime import Th5ConfigEntry

PARALLEL_UPDATES = 0

SUMMARY = ("The device installs it at its next sync (press the top button to sync now), "
           "then restarts. The new version shows after that.")


async def async_setup_entry(hass: HomeAssistant, entry: Th5ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([McuFirmwareUpdate(entry.runtime_data, "mcu_firmware")])


class McuFirmwareUpdate(Th5Entity, UpdateEntity):
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    @property
    def installed_version(self) -> str | None:
        v = self.runtime.mcu_fw
        return None if v is None else version_str(v)

    @property
    def latest_version(self) -> str | None:
        # No usable package: nothing newer than what runs.
        fw = self.runtime.fw_latest
        return fw.version_str if fw else self.installed_version

    @property
    def in_progress(self) -> bool:
        return self.runtime.fw_queued is not None

    @property
    def release_summary(self) -> str | None:
        if self.runtime.boot_ver == 0:
            return "The bootloader is not installed (boot_ver 0); install it over SWD once."
        queued = self.runtime.fw_queued
        if queued is not None:
            return f"Version {queued.version_str} is queued. {SUMMARY}"
        return SUMMARY

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"boot_ver": self.runtime.boot_ver}

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        fw = self.runtime.fw_latest
        if fw is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_firmware_bundled")
        await self.runtime.async_queue_firmware(fw.data)
