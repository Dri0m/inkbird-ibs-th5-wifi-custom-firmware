"""th5_link: the CBU side of the Inkbird IBS-TH5 MCU <-> CBU link.

Binding spec: docs/protocol.md. Constants, payload offsets and the CRC come
from firmware/src/link_proto.h, which this module copies into the build at
code-generation time (single source of truth; nothing is duplicated here).

It owns the UART to the HC32, publishes the HELLO_RSP readings, registers the
HA actions th5_config / th5_frame / th5_firmware / th5_done with responses, and
ends every session with an orderly shutdown followed by BYE.
"""

from pathlib import Path

import esphome.codegen as cg
from esphome.components import sensor, text_sensor, uart
from esphome.components.ota import request_ota_state_listeners
from esphome.components.safe_mode import SafeModeComponent
import esphome.config_validation as cv
from esphome.const import (
    CONF_BATTERY_LEVEL,
    CONF_BSSID,
    CONF_CHANNEL,
    CONF_NETWORKS,
    CONF_OTA,
    CONF_PASSWORD,
    CONF_SSID,
    CONF_WIFI,
    CONF_BATTERY_VOLTAGE,
    CONF_HUMIDITY,
    CONF_ID,
    CONF_TEMPERATURE,
    DEVICE_CLASS_BATTERY,
    DEVICE_CLASS_DURATION,
    DEVICE_CLASS_HUMIDITY,
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_VOLTAGE,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
    UNIT_CELSIUS,
    UNIT_MILLISECOND,
    UNIT_MILLIVOLT,
    UNIT_PERCENT,
)
from esphome.core import CORE
import esphome.final_validate as fv
from esphome.helpers import write_file_if_changed

CODEOWNERS = []
DEPENDENCIES = ["uart", "api", "wifi"]
AUTO_LOAD = ["sensor", "text_sensor", "json"]

th5_link_ns = cg.esphome_ns.namespace("th5_link")
Th5Link = th5_link_ns.class_("Th5Link", cg.Component, uart.UARTDevice)

CONF_FAST_BAUD = "fast_baud"
CONF_WIFI_TIMEOUT = "wifi_timeout"
CONF_API_TIMEOUT = "api_timeout"
CONF_HA_TIMEOUT = "ha_timeout"
CONF_API_DISCONNECT_TIMEOUT = "api_disconnect_timeout"
CONF_SESSION_TIMEOUT = "session_timeout"
CONF_SAFE_MODE_ID = "safe_mode_id"
CONF_PROTO_HEADER = "proto_header"
CONF_SESSION = "session"
CONF_UNITS = "units"
CONF_CHARGE_STATE = "charge_state"
CONF_RESET_CAUSE = "reset_cause"
CONF_LAST_RESULT = "last_result"
CONF_LAST_SESSION = "last_session"
CONF_FAIL_COUNT = "fail_count"
CONF_TRIGGER = "trigger"
CONF_MCU_VERSION = "mcu_version"
CONF_WIFI_JOIN = "wifi_join"
CONF_HISTORY = "history"

# esphome/components/th5_link/__init__.py -> project root -> firmware/src
DEFAULT_PROTO_HEADER = (
    Path(__file__).resolve().parents[3] / "firmware" / "src" / "link_proto.h"
)


def _proto_header(value):
    path = Path(cv.string(value)).expanduser()
    if not path.is_absolute():
        path = Path(CORE.config_dir) / path
    if not path.is_file():
        raise cv.Invalid(
            f"{path} not found: th5_link needs firmware/src/link_proto.h from this repo"
        )
    if not path.with_name("fwu.h").is_file():
        raise cv.Invalid(f"{path.with_name('fwu.h')} not found: th5_link needs it next to link_proto.h")
    return str(path)


_DIAG_TEXT = text_sensor.text_sensor_schema(entity_category=ENTITY_CATEGORY_DIAGNOSTIC)

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(Th5Link),
            # 500 kbaud after HELLO_RSP (protocol.md 2.3). Off by default: ESPHome's
            # LibreTiny UART has no load_settings(); th5_link reprograms the divider
            # with SerialClass::configure(), unverified on hardware. Off also sets
            # HELLO SLOW_ONLY, so the MCU does not offer 500 k.
            cv.Optional(CONF_FAST_BAUD, default=False): cv.boolean,
            cv.Optional(
                CONF_WIFI_TIMEOUT, default="10s"
            ): cv.positive_time_period_milliseconds,
            cv.Optional(
                CONF_API_TIMEOUT, default="10s"
            ): cv.positive_time_period_milliseconds,
            # design.md 3 step 9: no th5_config within 3 s of the API connecting.
            cv.Optional(
                CONF_HA_TIMEOUT, default="3s"
            ): cv.positive_time_period_milliseconds,
            # Soft session deadline from boot: HA has not called th5_done by then ->
            # push a complete buffered frame, BYE NO_HA. Well below the MCU's 45 s
            # cap (which ends as CAP), leaving room for the push at 115200 (~3 s).
            # 0 disables it. Not applied in bench, maintenance or OTA.
            cv.Optional(CONF_SESSION_TIMEOUT, default="25s"): cv.All(
                cv.positive_time_period_milliseconds,
                cv.Range(max=cv.TimePeriod(seconds=38)),
            ),
            cv.Optional(
                CONF_API_DISCONNECT_TIMEOUT, default="500ms"
            ): cv.All(
                cv.positive_time_period_milliseconds,
                cv.Range(max=cv.TimePeriod(milliseconds=1000)),
            ),
            cv.OnlyWith(CONF_SAFE_MODE_ID, "safe_mode"): cv.use_id(SafeModeComponent),
            cv.Optional(
                CONF_PROTO_HEADER, default=str(DEFAULT_PROTO_HEADER)
            ): _proto_header,
            cv.Optional(CONF_TEMPERATURE): sensor.sensor_schema(
                unit_of_measurement=UNIT_CELSIUS,
                accuracy_decimals=2,
                device_class=DEVICE_CLASS_TEMPERATURE,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_HUMIDITY): sensor.sensor_schema(
                unit_of_measurement=UNIT_PERCENT,
                accuracy_decimals=2,
                device_class=DEVICE_CLASS_HUMIDITY,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_BATTERY_LEVEL): sensor.sensor_schema(
                unit_of_measurement=UNIT_PERCENT,
                accuracy_decimals=0,
                device_class=DEVICE_CLASS_BATTERY,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_BATTERY_VOLTAGE): sensor.sensor_schema(
                unit_of_measurement=UNIT_MILLIVOLT,
                accuracy_decimals=0,
                device_class=DEVICE_CLASS_VOLTAGE,
                state_class=STATE_CLASS_MEASUREMENT,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            cv.Optional(CONF_LAST_SESSION): sensor.sensor_schema(
                unit_of_measurement=UNIT_MILLISECOND,
                accuracy_decimals=0,
                device_class=DEVICE_CLASS_DURATION,
                state_class=STATE_CLASS_MEASUREMENT,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            cv.Optional(CONF_FAIL_COUNT): sensor.sensor_schema(
                accuracy_decimals=0,
                state_class=STATE_CLASS_MEASUREMENT,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            # The integration's session trigger (docs/integration-contract.md).
            cv.Optional(CONF_SESSION): _DIAG_TEXT,
            cv.Optional(CONF_UNITS): text_sensor.text_sensor_schema(),
            cv.Optional(CONF_CHARGE_STATE): _DIAG_TEXT,
            cv.Optional(CONF_RESET_CAUSE): _DIAG_TEXT,
            cv.Optional(CONF_LAST_RESULT): _DIAG_TEXT,
            cv.Optional(CONF_TRIGGER): _DIAG_TEXT,
            cv.Optional(CONF_MCU_VERSION): _DIAG_TEXT,
            cv.Optional(CONF_WIFI_JOIN): _DIAG_TEXT,
            cv.Optional(CONF_HISTORY): _DIAG_TEXT,
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
    .extend(uart.UART_DEVICE_SCHEMA)
)

# The session always starts at 115200 8N1 (protocol.md 2.1).
_UART_FINAL_VALIDATE = uart.final_validate_device_schema(
    "th5_link",
    baud_rate=115200,
    require_tx=True,
    require_rx=True,
    data_bits=8,
    parity="NONE",
    stop_bits=1,
)

PLACEHOLDER_PREFIX = "REPLACE-"  # secrets.yaml.example


def _final_validate(config):
    _UART_FINAL_VALIDATE(config)
    full = fv.full_config.get()
    # Credentials remain compiled; AP discovery learns and recovers at runtime.
    for net in full.get(CONF_WIFI, {}).get(CONF_NETWORKS, []):
        password = str(net.get(CONF_PASSWORD, ""))
        if len(password) == 64 and any(c not in "0123456789abcdefABCDEF" for c in password):
            raise cv.Invalid("th5_link: a 64-character Wi-Fi key must be hexadecimal")
        for key in (CONF_SSID, CONF_PASSWORD):
            if str(net.get(key, "")).startswith(PLACEHOLDER_PREFIX):
                raise cv.Invalid(
                    f"th5_link: wifi {key} is still the secrets.yaml.example placeholder"
                )
    for ota_conf in full.get(CONF_OTA, []) or []:
        if str(ota_conf.get(CONF_PASSWORD, "")).startswith(PLACEHOLDER_PREFIX):
            raise cv.Invalid(
                "th5_link: ota password is still the secrets.yaml.example placeholder"
            )
    wifi_conf = full.get(CONF_WIFI, {})
    if not wifi_conf.get("fast_connect"):
        raise cv.Invalid("th5_link: fast_connect must be true for learned AP pinning")
    if len(wifi_conf.get(CONF_NETWORKS, [])) != 1:
        raise cv.Invalid("th5_link: learned AP pinning requires one Wi-Fi network")
    if any(net.get(CONF_BSSID) or net.get(CONF_CHANNEL) for net in wifi_conf.get(CONF_NETWORKS, [])):
        raise cv.Invalid("th5_link: remove fixed bssid/channel; the learned AP must recover by scanning")
    return config


FINAL_VALIDATE_SCHEMA = _final_validate

SENSORS = {
    CONF_TEMPERATURE: "set_temperature_sensor",
    CONF_HUMIDITY: "set_humidity_sensor",
    CONF_BATTERY_LEVEL: "set_battery_level_sensor",
    CONF_BATTERY_VOLTAGE: "set_battery_voltage_sensor",
    CONF_LAST_SESSION: "set_last_session_sensor",
    CONF_FAIL_COUNT: "set_fail_count_sensor",
}
TEXT_SENSORS = {
    CONF_UNITS: "set_units_text_sensor",
    CONF_CHARGE_STATE: "set_charge_state_text_sensor",
    CONF_RESET_CAUSE: "set_reset_cause_text_sensor",
    CONF_LAST_RESULT: "set_last_result_text_sensor",
    CONF_TRIGGER: "set_trigger_text_sensor",
    CONF_MCU_VERSION: "set_mcu_version_text_sensor",
    CONF_WIFI_JOIN: "set_wifi_join_text_sensor",
    CONF_HISTORY: "set_history_text_sensor",
    # Initial-state API synchronization follows registration order too.
    CONF_SESSION: "set_session_text_sensor",
}


async def to_code(config):
    # Build-local SDK repair: the installed framework remains untouched.
    patch = Path(CORE.relative_build_path("th5_sdk_patch.py"))
    write_file_if_changed(patch, Path(__file__).with_name("sdk_patch.py").read_text())
    cg.add_platformio_option("extra_scripts", [f"pre:{patch}"])
    # The shared protocol header goes into a directory of its own, so that no
    # other firmware header (flash.h, rtc.h, ...) can shadow an SDK header.
    inc = Path(CORE.relative_build_path("th5_link_include"))
    inc.mkdir(parents=True, exist_ok=True)
    write_file_if_changed(
        inc / "link_proto.h", Path(config[CONF_PROTO_HEADER]).read_text()
    )
    # The MCU firmware package header (th5_firmware), next to link_proto.h.
    write_file_if_changed(
        inc / "fwu.h", Path(config[CONF_PROTO_HEADER]).with_name("fwu.h").read_text()
    )
    cg.add_build_flag(f"-I{inc}")

    # The actions are registered from C++ (api::register_user_service), which
    # needs the user-defined-action machinery with responses compiled in.
    cg.add_define("USE_API_USER_DEFINED_ACTIONS")
    cg.add_define("USE_API_CUSTOM_SERVICES")
    cg.add_define("USE_API_USER_DEFINED_ACTION_RESPONSES")
    # NET OTA (protocol.md 4.4) when an OTA upload starts.
    request_ota_state_listeners()

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await uart.register_uart_device(var, config)

    cg.add(var.set_fast_baud(config[CONF_FAST_BAUD]))
    cg.add(var.set_wifi_timeout(config[CONF_WIFI_TIMEOUT]))
    cg.add(var.set_api_timeout(config[CONF_API_TIMEOUT]))
    cg.add(var.set_ha_timeout(config[CONF_HA_TIMEOUT]))
    cg.add(var.set_api_disconnect_timeout(config[CONF_API_DISCONNECT_TIMEOUT]))
    cg.add(var.set_session_timeout(config[CONF_SESSION_TIMEOUT]))
    if CONF_SAFE_MODE_ID in config:
        cg.add_define("TH5_LINK_SAFE_MODE")
        sm = await cg.get_variable(config[CONF_SAFE_MODE_ID])
        cg.add(var.set_safe_mode(sm))

    for key, setter in SENSORS.items():
        if key in config:
            s = await sensor.new_sensor(config[key])
            cg.add(getattr(var, setter)(s))
    for key, setter in TEXT_SENSORS.items():
        if key in config:
            s = await text_sensor.new_text_sensor(config[key])
            cg.add(getattr(var, setter)(s))
