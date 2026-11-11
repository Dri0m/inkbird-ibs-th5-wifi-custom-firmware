"""The MCU firmware package (.fwu, firmware/tools/mkfwu.py; design 4.7).

32-byte header (little-endian): magic "TH5U", fw_version (major<<24 |
minor<<16 | patch), image length, image crc32 (zlib), load address 0x1000,
min_boot_ver, two erased words (commit, done); then the app image. The whole
package goes through th5_firmware; the CBU checks it again and the MCU
checks the CRC over flash.

Pure Python, stdlib only (tests and ha/tools import it without Home Assistant).
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"TH5U"
HEADER = 32
LOAD = 0x1000
MAX_IMAGE = 50144           # the staging area (50 176 B) minus the header
MIN_IMAGE = 64
MAX_PACKAGE = HEADER + MAX_IMAGE
ERASED = 0xFFFFFFFF

BUNDLED = Path(__file__).parent / "firmware" / "th5.fwu"


class FirmwareError(ValueError):
    """Not a usable .fwu package."""


@dataclass(frozen=True, slots=True)
class Fwu:
    data: bytes             # the whole package, as sent
    version: int            # fw_version
    min_boot_ver: int

    @property
    def version_str(self) -> str:
        return version_str(self.version)


def version_str(v: int) -> str:
    """fw_version as "major.minor.patch" (8, 8 and 16 bits)."""
    return f"{v >> 24 & 0xFF}.{v >> 16 & 0xFF}.{v & 0xFFFF}"


def parse_fwu(data: bytes) -> Fwu:
    """Check a package (magic, sizes, load address, erased commit/done, CRC)."""
    if len(data) < HEADER:
        raise FirmwareError("shorter than the 32-byte header")
    if data[:4] != MAGIC:
        raise FirmwareError("not a TH5 firmware package (magic is not TH5U)")
    version, length, crc, load, min_boot, commit, done = struct.unpack_from("<7I", data, 4)
    if not MIN_IMAGE <= length <= MAX_IMAGE:
        raise FirmwareError(f"image length {length} B is outside {MIN_IMAGE}..{MAX_IMAGE}")
    if len(data) != HEADER + length:
        raise FirmwareError(f"{len(data)} B in the file, the header says {HEADER + length}")
    if load != LOAD:
        raise FirmwareError(f"load address {load:#x}, expected {LOAD:#x}")
    if (commit, done) != (ERASED, ERASED):
        raise FirmwareError("commit or done word is not erased")
    if zlib.crc32(data[HEADER:]) != crc:
        raise FirmwareError("image CRC mismatch")
    return Fwu(bytes(data), version, min_boot)


def load_bundled() -> Fwu | None:
    """The package shipped with the integration; None when it is not there.

    Raises FirmwareError when it is there but invalid."""
    try:
        data = BUNDLED.read_bytes()
    except FileNotFoundError:
        return None
    return parse_fwu(data)


def path_inside(base: str, path: str) -> Path | None:
    """`path` (relative to `base`, or absolute) resolved, or None if it leaves `base`."""
    root = Path(base).resolve()
    p = (root / path).resolve()
    return p if p.is_relative_to(root) else None
