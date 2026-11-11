"""One sync with the device: the HA side of docs/integration-contract.md "Integration
contract".

The device publishes its `session` text sensor (nonce;units;frame_id;
settings_crc;boot) once the API is up; HA then has 3 s per call to send
th5_config, the frame in chunks if the device's frame differs, and th5_done.

Pure Python, stdlib only: the HA glue passes an async `call(action, data)`,
so tests drive this without Home Assistant.
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime as dt
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .timeinfo import time_fields

CHUNK = 4096                    # raw bytes per th5_frame (5 464 base64 chars)

# Settings the integration owns, with the MCU's defaults (settings.c,
# protocol.md 5.3). All are sent at every sync.
DEFAULTS: dict[str, Any] = {
    "mode": "forecast",         # or "picture"
    "sync_min": 60,             # 15..240
    "clock_min": 1,             # 1..30
    "clock_24h": True,
    "full_n": 30,               # band updates per full refresh, 1..255
    "t_off": 0.0,               # degC, -10..10
    "rh_off": 0.0,              # %RH, -20..20
    "top_bar": False,           # picture mode: live top bar above the picture (MCU MODE forecast)
    "maintenance": False,       # keep the CBU up (HOLD 900) for OTA
    "monochrome": False,        # forecast band text: greyscale by default
    "single_reset": False,      # experimental: one reset per greyscale band update
}

# fw_version uses major<<24 | minor<<16 | patch (8/8/16 bits).
BAND_OPTIONS_FW = 0x00020002    # 0.2.2: CONFIG FLAGS bits 2 and 3

# Inclusive ranges of the numeric settings, as the MCU's CONFIG checks them.
RANGES: dict[str, tuple[float, float]] = {
    "sync_min": (15, 240),
    "clock_min": (1, 30),
    "full_n": (1, 255),
    "t_off": (-10.0, 10.0),
    "rh_off": (-20.0, 20.0),
}


class CallError(Exception):
    """An action the device refused or that never came back."""


@dataclass(frozen=True)
class Hello:
    """The device's `session` sensor."""
    nonce: str
    units: str                  # "C" or "F"
    frame_id: int               # 0 = no frame stored
    settings_crc: int
    boot: int | None = None
    mcu_fw: int | None = None   # fw_version of the MCU app (8 hex digits), None from an old CBU
    boot_ver: int | None = None  # bootloader version (2 hex digits), 0 = none


def parse_session(value: str | None) -> Hello | None:
    """`nonce;units;frame_id;settings_crc[;boot[;mcu_fw;boot_ver...]]` -> Hello,
    None if malformed. Later fields are ignored (docs/integration-contract.md: parse by position);
    the optional ones are None when absent, and mcu_fw/boot_ver also when
    unreadable (an old CBU)."""
    p = (value or "").strip().split(";")
    if len(p) < 4 or not p[0] or p[1] not in ("C", "F"):
        return None

    def field(i: int) -> int | None:
        return int(p[i], 16) if len(p) > i and p[i] else None

    try:
        hello = Hello(p[0], p[1], int(p[2], 16), int(p[3], 16), field(4))
    except ValueError:
        return None
    for name, i in (("mcu_fw", 5), ("boot_ver", 6)):
        with contextlib.suppress(ValueError):
            hello = dataclasses.replace(hello, **{name: field(i)})
    return hello


def _clamp(key: str, v: float) -> float:
    lo, hi = RANGES[key]
    return min(max(v, lo), hi)


def build_config(settings: Mapping[str, Any], tz: dt.tzinfo, utc: int,
                 *, mcu_fw: int | None = None) -> dict[str, Any]:
    """The th5_config JSON object: time with the next DST change, and every setting.

    Numbers are clamped into RANGES, so a hand-edited store cannot get the
    whole CONFIG refused.
    New FLAGS bits are omitted until the session confirms MCU 0.2.2 or later:
    an older MCU refuses unknown bits, including during an upgrade session."""
    s = {**DEFAULTS, **settings}
    off, nxt, nxt_off = time_fields(tz, utc)
    config = {
        "utc": utc, "offset_min": off, "next_change_utc": nxt, "next_offset_min": nxt_off,
        # The MCU draws its live band over any band-flagged frame in MODE
        # forecast; a picture with the top bar is such a frame (protocol 5.3).
        "mode": "forecast" if s["mode"] == "picture" and s["top_bar"] else s["mode"],
        "sync_min": int(_clamp("sync_min", int(s["sync_min"]))),
        "clock_min": int(_clamp("clock_min", int(s["clock_min"]))),
        "clock_24h": bool(s["clock_24h"]),
        "full_n": int(_clamp("full_n", int(s["full_n"]))),
        "t_off": round(_clamp("t_off", float(s["t_off"])) * 100),
        "rh_off": round(_clamp("rh_off", float(s["rh_off"])) * 100),
        # Send the shared FLAGS settings together, so one change keeps the others.
        # FLAGS bit 0 (BADGE) is no longer used by HA: always 0.
        "badge": False, "maintenance": bool(s["maintenance"]),
    }
    if mcu_fw is not None and mcu_fw >= BAND_OPTIONS_FW:
        config.update(monochrome=bool(s["monochrome"]), single_reset=bool(s["single_reset"]))
    return config


@dataclass
class SyncResult:
    ok: bool
    frame_sent: bool = False    # every chunk accepted; the commit shows in the next session
    firmware_sent: bool = False  # every th5_firmware chunk accepted; the CBU pushes after th5_done
    error: str | None = None
    failed_action: str | None = None


Call = Callable[[str, dict[str, Any]], Awaitable[None]]


def chunks(slot: bytes, size: int = CHUNK) -> list[tuple[int, str]]:
    """(offset, base64 data) for each th5_frame / th5_firmware call, offset 0
    first, contiguous."""
    return [(off, base64.b64encode(slot[off:off + size]).decode("ascii"))
            for off in range(0, len(slot), size)]


async def run_sync(call: Call, hello: Hello, config: Mapping[str, Any],
                   slot: bytes | None, slot_id: int,
                   firmware: bytes | None = None) -> SyncResult:
    """th5_config, th5_frame x n (only if the device's frame differs), th5_done.

    firmware: a whole .fwu package. It replaces the frame (never both): the
    CBU pushes it to the MCU after th5_done and sends no frame in that session.

    slot: the whole slot image (32-byte header + payload) for the device's
    units and the current mode, or None to send no frame. On a failure the
    rest is skipped, except that th5_done is still tried (unless it was
    th5_done that failed) so the device turns its radio off at once instead
    of waiting out its 3 s window."""
    sent = fw_sent = False
    action = "th5_config"
    try:
        await call(action, {"json": json.dumps(config, separators=(",", ":"))})
        if firmware is not None:
            action = "th5_firmware"
            for off, data in chunks(firmware):
                await call(action, {"offset": off, "data": data})
            fw_sent = True
        elif slot is not None and slot_id != hello.frame_id:
            action = "th5_frame"
            for off, data in chunks(slot):
                await call(action, {"offset": off, "data": data})
            sent = True
        action = "th5_done"
        await call(action, {})
    except CallError as e:
        if action != "th5_done":
            with contextlib.suppress(CallError):
                await call("th5_done", {})
        return SyncResult(False, sent, fw_sent, str(e), action)
    return SyncResult(True, sent, fw_sent)
