"""session.py without Home Assistant: parsing, config JSON, the call sequence.

    uv run --with pytest --with pillow pytest ha/tests/test_link_session.py
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

HA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HA.parent / "custom_components"))

from th5_display import frame, session  # noqa: E402
from th5_display.session import CallError, Hello, build_config, parse_session, run_sync  # noqa: E402

TZ = ZoneInfo("Europe/Oslo")


def slot_of(grey: int = 170, flags: int = frame.FRAME_FLAG_BAND) -> tuple[bytes, int]:
    payload = frame.encode_planes(Image.new("L", (frame.W, frame.H), grey), 2)
    fid = frame.frame_id_of(payload, 2, flags)
    return frame.build_slot(payload, 2, flags=flags, frame_id=fid, commit=False), fid


class Device:
    """Records calls; `fail` maps an action to the error it raises (once per call)."""

    def __init__(self, fail: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail or {}

    async def __call__(self, action: str, data: dict) -> None:
        self.calls.append((action, data))
        if action in self.fail:
            raise CallError(f"{action}: {self.fail[action]}")

    @property
    def actions(self) -> list[str]:
        return [a for a, _ in self.calls]


def run(dev: Device, hello: Hello, slot: bytes | None, fid: int) -> session.SyncResult:
    return asyncio.run(run_sync(dev, hello, {"utc": 1}, slot, fid))


# --------------------------------------------------------------------------- parse_session


def test_parse_session_five_fields():
    h = parse_session("0badc0de;F;00000000000000ff;1234abcd;0000002a")
    assert h == Hello("0badc0de", "F", 0xFF, 0x1234ABCD, 42)


def test_parse_session_unreadable_boot_ver_keeps_mcu_fw():
    h = parse_session("01;C;0;0;2a;00020004;xx")
    assert (h.mcu_fw, h.boot_ver) == (0x00020004, None)


def test_parse_session_four_fields_and_extra_fields():
    assert parse_session("01;C;0;0").boot is None
    h = parse_session("01;C;10;20;30;future;fields")
    assert (h.frame_id, h.settings_crc, h.boot) == (0x10, 0x20, 0x30)


@pytest.mark.parametrize("value", [None, "", "a;C;1", "a;K;1;2", ";C;1;2", "a;C;xyz;2", "a;C;1;2;zz"])
def test_parse_session_rejects(value):
    assert parse_session(value) is None


# --------------------------------------------------------------------------- build_config


def test_build_config_fields_and_units():
    utc = int(dt.datetime(2026, 11, 2, 12, tzinfo=dt.UTC).timestamp())
    c = build_config({"t_off": -0.3, "rh_off": 2, "mode": "picture", "badge": True}, TZ, utc)
    assert c["utc"] == utc
    assert c["offset_min"] == 60 and c["next_offset_min"] == 120
    # 2027-03-28 01:00 UTC: CET -> CEST
    assert c["next_change_utc"] == int(dt.datetime(2027, 3, 28, 1, tzinfo=dt.UTC).timestamp())
    assert c["t_off"] == -30 and c["rh_off"] == 200       # 0.01 units
    # a stray old "badge" setting is ignored: FLAGS bit 0 is always off now
    assert c["mode"] == "picture" and c["badge"] is False and c["maintenance"] is False
    assert set(c) == {"utc", "offset_min", "next_change_utc", "next_offset_min", "mode", "sync_min",
                      "clock_min", "clock_24h", "full_n", "t_off", "rh_off", "badge", "maintenance"}
    # compact JSON stays well under the API message limit
    assert len(json.dumps(c, separators=(",", ":"))) < 400


def test_build_config_defaults_and_clamping():
    c = build_config({}, TZ, 0)
    assert (c["sync_min"], c["clock_min"], c["full_n"], c["clock_24h"]) == (60, 1, 30, True)
    c = build_config({"sync_min": 5, "clock_min": 99, "full_n": 0, "t_off": 50, "rh_off": -50},
                     TZ, 0)
    assert (c["sync_min"], c["clock_min"], c["full_n"]) == (15, 30, 1)
    assert (c["t_off"], c["rh_off"]) == (1000, -2000)


@pytest.mark.parametrize("version", [None, 0x00000202, 0x00020000, 0x00020001])
def test_band_options_not_sent_to_legacy_mcu(version):
    c = build_config({"monochrome": True, "single_reset": True,
                      "maintenance": True}, TZ, 0, mcu_fw=version)
    assert "monochrome" not in c and "single_reset" not in c
    assert c["badge"] is False and c["maintenance"] is True


@pytest.mark.parametrize("version", [0x00020002, 0x00030000, 0x01000000])
def test_band_options_defaults_and_shared_flags(version):
    c = build_config({}, TZ, 0, mcu_fw=version)
    assert c["monochrome"] is False and c["single_reset"] is False
    c = build_config({"monochrome": True, "single_reset": True,
                      "maintenance": True}, TZ, 0, mcu_fw=version)
    assert all(c[k] is True for k in ("monochrome", "single_reset", "maintenance"))
    c = build_config({"single_reset": True}, TZ, 0, mcu_fw=version)
    assert c["single_reset"] is True and c["monochrome"] is False


@pytest.mark.parametrize(("mode", "top_bar", "sent"), [
    ("picture", False, "picture"), ("picture", True, "forecast"),
    ("forecast", True, "forecast"), ("forecast", False, "forecast")])
def test_picture_top_bar_is_sent_as_mcu_forecast_mode(mode, top_bar, sent):
    # MCU MODE 1 draws the live band over any FRAME_FLAG_BAND frame (policy.c band_live)
    c = build_config({"mode": mode, "top_bar": top_bar}, TZ, 0)
    assert c["mode"] == sent and c["badge"] is False and "top_bar" not in c


# --------------------------------------------------------------------------- run_sync


def test_frame_sent_in_contiguous_chunks():
    slot, fid = slot_of()
    assert len(slot) == frame.FRAME_HDR_BYTES + 2 * frame.FRAME_PLANE_BYTES == 24_992
    dev = Device()
    res = run(dev, Hello("n", "C", 0, 0), slot, fid)
    assert res.ok and res.frame_sent and res.error is None
    assert dev.actions == ["th5_config"] + ["th5_frame"] * 7 + ["th5_done"]
    frames = [d for a, d in dev.calls if a == "th5_frame"]
    assert [d["offset"] for d in frames] == list(range(0, 7 * session.CHUNK, session.CHUNK))
    assert all(len(d["data"]) <= 5464 for d in frames)
    assert b"".join(base64.b64decode(d["data"]) for d in frames) == slot
    assert json.loads(dev.calls[0][1]["json"]) == {"utc": 1}
    assert dev.calls[-1] == ("th5_done", {})


def test_frame_skipped_when_ids_match():
    slot, fid = slot_of()
    dev = Device()
    res = run(dev, Hello("n", "C", fid, 0), slot, fid)
    assert res.ok and not res.frame_sent
    assert dev.actions == ["th5_config", "th5_done"]


def test_no_slot_sends_no_frame():
    dev = Device()
    assert run(dev, Hello("n", "C", 0, 0), None, 0).ok
    assert dev.actions == ["th5_config", "th5_done"]


def test_config_failure_still_sends_done():
    slot, fid = slot_of()
    dev = Device({"th5_config": "TIME refused: BAD_VALUE"})
    res = run(dev, Hello("n", "C", 0, 0), slot, fid)
    assert not res.ok and not res.frame_sent
    assert res.failed_action == "th5_config" and "BAD_VALUE" in res.error
    assert dev.actions == ["th5_config", "th5_done"]


def test_frame_failure_stops_upload_and_sends_done():
    slot, fid = slot_of()
    dev = Device({"th5_frame": "bad header"})
    res = run(dev, Hello("n", "F", 0, 0), slot, fid)
    assert not res.ok and not res.frame_sent and res.failed_action == "th5_frame"
    assert dev.actions == ["th5_config", "th5_frame", "th5_done"]


def test_done_failure_is_not_retried():
    dev = Device({"th5_done": "timeout"})
    res = run(dev, Hello("n", "C", 0, 0), None, 0)
    assert not res.ok and res.failed_action == "th5_done"
    assert dev.actions == ["th5_config", "th5_done"]


def test_done_error_after_failure_is_swallowed():
    dev = Device({"th5_config": "x", "th5_done": "y"})
    res = run(dev, Hello("n", "C", 0, 0), None, 0)
    assert not res.ok and res.error == "th5_config: x"
