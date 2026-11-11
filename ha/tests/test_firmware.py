"""The .fwu parser and the session's firmware plan, without Home Assistant.

    uv run --with pytest pytest -q ha/tests/test_firmware.py
"""
from __future__ import annotations

import asyncio
import base64
import struct
import sys
import zlib
from pathlib import Path

import pytest

HA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HA.parent / "custom_components"))

from th5_display import firmware, session  # noqa: E402
from th5_display.firmware import FirmwareError, parse_fwu, path_inside, version_str  # noqa: E402
from th5_display.session import CHUNK, CallError, Hello, parse_session, run_sync  # noqa: E402


def make_fwu(version: int = 0x01020003, size: int = 9000, min_boot: int = 1) -> bytes:
    """What firmware/tools/mkfwu.py writes."""
    image = bytes((i * 7) & 0xFF for i in range(size))
    return b"TH5U" + struct.pack("<7I", version, len(image), zlib.crc32(image), 0x1000,
                                 min_boot, 0xFFFFFFFF, 0xFFFFFFFF) + image


class Device:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def __call__(self, action: str, data: dict) -> None:
        self.calls.append((action, data))
        if action == self.fail:
            raise CallError(f"{action}: refused")

    @property
    def actions(self) -> list[str]:
        return [a for a, _ in self.calls]


HELLO = Hello("0badc0de", "C", 0xFF, 0x1234, 1, 0x01020003, 1)


# --------------------------------------------------------------------------- .fwu


def test_parse_valid_package():
    pkg = parse_fwu(make_fwu())
    assert (pkg.version, pkg.min_boot_ver, pkg.version_str) == (0x01020003, 1, "1.2.3")
    assert len(pkg.data) == 32 + 9000


def test_version_string_field_widths():
    assert version_str(0x0A0B1234) == "10.11.4660"
    assert version_str(0) == "0.0.0"


@pytest.mark.parametrize("mutate, text", [
    (lambda b: b"XXXX" + b[4:], "magic"),
    (lambda b: b[:20], "header"),
    (lambda b: b[:-1], "header says"),
    (lambda b: b + b"\0", "header says"),
    (lambda b: b[:-1] + bytes([b[-1] ^ 1]), "CRC"),
    (lambda b: b[:16] + struct.pack("<I", 0) + b[20:], "load"),
    (lambda b: b[:24] + struct.pack("<I", 0) + b[28:], "erased"),
    (lambda b: b[:8] + struct.pack("<I", 60000) + b[12:], "length"),
])
def test_parse_rejects(mutate, text):
    with pytest.raises(FirmwareError, match=text):
        parse_fwu(mutate(make_fwu()))


def test_parse_accepts_the_largest_image():
    assert len(parse_fwu(make_fwu(size=firmware.MAX_IMAGE)).data) == firmware.MAX_PACKAGE


def test_bundled_package_is_absent_or_valid():
    # None when not shipped; if it is shipped it must parse (raises otherwise).
    pkg = firmware.load_bundled()
    assert pkg is None or pkg.min_boot_ver >= 1


def test_path_inside_config_dir(tmp_path):
    (tmp_path / "x.fwu").write_bytes(b"")
    assert path_inside(str(tmp_path), "x.fwu") == (tmp_path / "x.fwu").resolve()
    assert path_inside(str(tmp_path), str(tmp_path / "x.fwu")) == (tmp_path / "x.fwu").resolve()
    assert path_inside(str(tmp_path), "../x.fwu") is None
    assert path_inside(str(tmp_path), "/etc/passwd") is None
    (tmp_path / "link").symlink_to("/etc")
    assert path_inside(str(tmp_path), "link/passwd") is None


# --------------------------------------------------------------------------- session


def test_parse_session_new_fields():
    h = parse_session("0badc0de;F;00000000000000ff;1234abcd;0000002a;01020003;01")
    assert (h.boot, h.mcu_fw, h.boot_ver) == (42, 0x01020003, 1)
    assert parse_session("0badc0de;F;00000000000000ff;1234abcd;0000002a;01020003;01;extra").boot_ver == 1


def test_parse_session_without_or_with_bad_new_fields():
    old = parse_session("0badc0de;F;00000000000000ff;1234abcd;0000002a")
    assert (old.mcu_fw, old.boot_ver) == (None, None)
    bad = parse_session("0badc0de;F;00000000000000ff;1234abcd;0000002a;zz;")
    assert bad is not None and (bad.mcu_fw, bad.boot_ver) == (None, None)
    assert parse_session("0badc0de;F;00000000000000ff;1234abcd;0000002a;;00").boot_ver == 0


def test_firmware_replaces_the_frame_in_contiguous_chunks():
    pkg = make_fwu(size=10000)
    dev = Device()
    res = asyncio.run(run_sync(dev, HELLO, {"utc": 1}, b"F" * 5000, 7, pkg))
    assert res.ok and res.firmware_sent and not res.frame_sent
    assert dev.actions[0] == "th5_config" and dev.actions[-1] == "th5_done"
    assert "th5_frame" not in dev.actions                       # never both
    fw = [d for a, d in dev.calls if a == "th5_firmware"]
    assert len(fw) == -(-len(pkg) // CHUNK)
    assert [d["offset"] for d in fw] == list(range(0, len(pkg), CHUNK))
    assert b"".join(base64.b64decode(d["data"]) for d in fw) == pkg


def test_no_firmware_leaves_the_frame_flow_untouched():
    dev = Device()
    res = asyncio.run(run_sync(dev, HELLO, {"utc": 1}, b"F" * 4000, 7))
    assert res.frame_sent and not res.firmware_sent
    assert dev.actions == ["th5_config", "th5_frame", "th5_done"]


def test_refused_firmware_still_ends_the_session():
    dev = Device(fail="th5_firmware")
    res = asyncio.run(run_sync(dev, HELLO, {"utc": 1}, None, 0, make_fwu()))
    assert not res.ok and not res.firmware_sent and res.failed_action == "th5_firmware"
    assert dev.actions[-1] == "th5_done"
