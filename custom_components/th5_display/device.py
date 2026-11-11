"""Find the ESPHome side of a TH5: node name, actions, MAC and its entities.

Used by the config flow (to check a picked device) and at setup. Nothing
here imports the esphome integration: it reads the device and entity
registries and the ESPHome config entry's data, so it keeps working while
the device sleeps (ESPHome restores its actions and entities from storage).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import ACTIONS, ESPHOME_DEVICE_NAME, ESPHOME_DOMAIN


class LinkError(Exception):
    """The device is not an ESPHome TH5; `reason` is a config flow error key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DeviceLink:
    """The ESPHome device a TH5 Display entry drives."""

    device_id: str
    mac: str                        # dr.format_mac: aa:bb:cc:dd:ee:ff
    node: str                       # action prefix: node name with '-' -> '_'
    name: str
    session_entity_id: str
    temperature_entity_id: str | None
    humidity_entity_id: str | None
    history_entity_id: str | None = None

    def action(self, name: str) -> str:
        """The esphome.* action name for th5_config / th5_frame / th5_done."""
        return f"{self.node}_{name}"


# ESPHome entity unique ids, by aioesphomeapi's build_unique_id version:
#   1  aabbccddeeff-text_sensor-session      (mac-type-object_id)
#   2  aabbccddeeff-text_sensor-Session      (mac-type-name)
#   3  AA:BB:CC:DD:EE:FF/0/text_sensor/Session   (mac/sub-device/type/name, HA 2026.x)
# Entries migrated from older HA versions can carry any of them.
_UID = re.compile(r"[-/](?P<type>text_sensor|sensor)[-/](?P<name>[^/]+)$")


def _uid_key(unique_id: str) -> tuple[str, str] | None:
    """(entity type, normalised name) from an ESPHome unique id."""
    m = _UID.search(unique_id)
    if m is None:
        return None
    return m["type"], m["name"].casefold().replace(" ", "_")


def _find(entries: list[er.RegistryEntry], esp_type: str, name: str) -> str | None:
    """Entity id of the ESPHome entity `esp_type`/`name` ("text_sensor", "session")."""
    for e in entries:
        if _uid_key(e.unique_id) == (esp_type, name):
            return e.entity_id
    # Fallback: the name the firmware gives it, whatever the unique id format.
    for e in entries:
        if (e.original_name or "").casefold().replace(" ", "_") == name:
            return e.entity_id
    return None


def async_resolve_link(hass: HomeAssistant, device_id: str) -> DeviceLink:
    """DeviceLink for an ESPHome device running th5_link, or raise LinkError."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise LinkError("device_not_found")
    if not isinstance(device, dr.DeviceEntry):     # a child device: never an ESPHome node
        raise LinkError("not_esphome")
    esp_entry = hass.config_entries.async_get_entry(device.config_entry_id)
    if esp_entry is None or esp_entry.domain != ESPHOME_DOMAIN:
        raise LinkError("not_esphome")
    node_name = esp_entry.data.get(ESPHOME_DEVICE_NAME)
    if not node_name:
        raise LinkError("not_esphome")
    node = node_name.replace("-", "_")
    if not all(hass.services.has_service(ESPHOME_DOMAIN, f"{node}_{a}") for a in ACTIONS):
        raise LinkError("no_th5_link")

    entries = [
        e for e in er.async_entries_for_device(er.async_get(hass), device_id)
        if e.platform == ESPHOME_DOMAIN and e.domain == Platform.SENSOR
    ]
    session = _find(entries, "text_sensor", "session")
    if session is None:
        raise LinkError("no_session_sensor")

    mac = next((v for k, v in device.connections if k == dr.CONNECTION_NETWORK_MAC),
               esp_entry.unique_id)
    if not mac:
        raise LinkError("not_esphome")
    return DeviceLink(
        device_id=device_id,
        mac=dr.format_mac(mac),
        node=node,
        name=device.name_by_user or device.name or node_name,
        session_entity_id=session,
        temperature_entity_id=_find(entries, "sensor", "temperature"),
        humidity_entity_id=_find(entries, "sensor", "humidity"),
        history_entity_id=_find(entries, "text_sensor", "history"),
    )
