"""Constants for the TH5 Display integration."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Final

DOMAIN: Final = "th5_display"
LOGGER = logging.getLogger(__package__)

ESPHOME_DOMAIN: Final = "esphome"
# ESPHome config entry data key holding the node name (esphome/const.py
# CONF_DEVICE_NAME); its actions are esphome.<node with - as _>_<action>.
ESPHOME_DEVICE_NAME: Final = "device_name"
ACTIONS: Final = ("th5_config", "th5_frame", "th5_done")

CONF_DEVICE_ID: Final = "device_id"
CONF_WEATHER_ENTITY: Final = "weather_entity"
CONF_LOCATION: Final = "location"

# Settings keys (session.DEFAULTS)
MODE_FORECAST: Final = "forecast"
MODE_PICTURE: Final = "picture"

# Timing (docs/integration-contract.md "Integration contract"). The device gives HA
# 3 s after the session sensor changes and 3 s after each answered call,
# and closes by itself about 20 s after the trigger.
CALL_TIMEOUT: Final = 10.0      # one action; th5_done in maintenance answers after the frame push
SESSION_BUDGET: Final = 20.0    # the whole sync, from the trigger
DONE_GRACE: Final = 2.0         # th5_done is still tried this long after the budget is spent
HOLD: Final = dt.timedelta(seconds=900)   # maintenance: the MCU's HOLD from the session start

FORECAST_INTERVAL: Final = dt.timedelta(minutes=30)
STORAGE_VERSION: Final = 1

ATTR_MEDIA: Final = "media"
ATTR_FIT: Final = "fit"
ATTR_DITHER: Final = "dither"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

SERVICE_SHOW_IMAGE: Final = "show_image"
SERVICE_REFRESH: Final = "refresh"
SERVICE_INSTALL_FIRMWARE: Final = "install_firmware"
ATTR_PATH: Final = "path"
