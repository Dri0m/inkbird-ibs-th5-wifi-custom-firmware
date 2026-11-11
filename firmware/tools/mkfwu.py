#!/usr/bin/env python3
"""Package the app for an update over the link, and join it with the
bootloader for SWD (design 4.7, src/fwu.h).

    mkfwu.py build/boot/boot.bin build/nodeep/th5.bin build/nodeep

writes, next to the app:
    th5.fwu       32 B staging header (commit/done erased) + the app image:
                  what th5_display sends through th5_firmware
    th5-full.bin  bootloader padded to 4 KB + the app: the SWD image at 0
"""
import struct
import sys
import zlib
from pathlib import Path

APP_BASE, BOOT_BYTES, INFO = 0x1000, 0x1000, 0xC0
MAX_IMAGE, MIN_IMAGE = 50144, 64
FWU_MAGIC, BOOT_MAGIC, APP_MAGIC = b"TH5U", b"TH5B", b"TH5A"
BOOT_VER = 1  # min_boot_ver: the bootloader version this package needs


def main():
    boot = Path(sys.argv[1]).read_bytes()
    app = Path(sys.argv[2]).read_bytes()
    out = Path(sys.argv[3])
    if len(boot) > BOOT_BYTES or boot[INFO:INFO + 4] != BOOT_MAGIC:
        sys.exit("bootloader: over 4 KB or no TH5B info block")
    if app[INFO:INFO + 4] != APP_MAGIC:
        sys.exit("app: no TH5A info block at 0xC0 (linked for 0x1000?)")
    app += b"\xff" * (-len(app) % 4)
    if not MIN_IMAGE <= len(app) <= MAX_IMAGE:
        sys.exit(f"app is {len(app)} B; the staging area takes {MIN_IMAGE}..{MAX_IMAGE}")
    sp, reset = struct.unpack_from("<II", app)
    if not 0x20000000 < sp <= 0x20004000 or not reset & 1 or not APP_BASE <= reset < APP_BASE + len(app):
        sys.exit(f"app vectors look wrong: sp {sp:#x} reset {reset:#x}")
    version = struct.unpack_from("<I", app, INFO + 4)[0]
    hdr = FWU_MAGIC + struct.pack("<7I", version, len(app), zlib.crc32(app), APP_BASE, BOOT_VER,
                                  0xFFFFFFFF, 0xFFFFFFFF)
    (out / "th5.fwu").write_bytes(hdr + app)
    (out / "th5-full.bin").write_bytes(boot.ljust(BOOT_BYTES, b"\xff") + app)
    v = f"{version >> 24}.{version >> 16 & 0xFF}.{version & 0xFFFF}"
    print(f"th5.fwu: v{v}, {len(app)} B, crc32 {zlib.crc32(app):08x}")


if __name__ == "__main__":
    main()
