# /// script
# requires-python = ">=3.11"
# dependencies = ["pyocd==0.46.*"]
# ///
"""Flash, verify and restore the HC32L170 over SWD (pyOCD + the HDSC pack:
`pyocd pack install hc32l170faua`, or --pack path/to/HC32L17X.pack).

    uv run tools/hc32_flash.py write firmware/build/nodeep/th5-full.bin  # bootloader + app
    uv run tools/hc32_flash.py frame frame-C.slot             # frame slot A (0x10000)
    uv run tools/hc32_flash.py restore backup/hc32-stock-1.bin  # your stock backup, all 128 KB
    uv run tools/hc32_flash.py read out.bin
    uv run tools/hc32_flash.py sector-test                    # program/erase 0x1D000

Safety (design 4.1): sector erase only, never chip erase, never any
protection/lock write. Every write is read back and compared.

The stock firmware's watchdog survives the debugger reset and counts while
the flash algorithm runs (~1.6 s budget). A reset gates off the WDT bus clock,
which silently drops feeds, so before every algorithm call this tool turns the
gate back on and feeds over SWD.
"""
import argparse
import hashlib
import struct
import sys
import time
import zlib
from pathlib import Path

# pyocd is imported where it is used, so the image checks import without it.

ROOT = Path(__file__).resolve().parent.parent
# Stock MCU firmware 1.0.5 as read from two REV1.2 units (identical). `restore`
# only tells you whether your backup is this image; it restores either way.
STOCK_SHA256 = "63c59a16e54f3f1bef44aed4fcc01660416ab2f176088220e04650f69fbced8f"
PACK_NAME = "HC32L17X.pack"
FLASH_SIZE = 0x20000
SECTOR = 0x200
CODE_LIMIT = 0x10000  # design 4.4: code and fonts only below 64 KB
SLOT_A, SLOT_B, SLOT_BYTES = 0x10000, 0x16200, 0x6200  # firmware/src/frame.h
FRAME_MAGIC = b"TH5F"
SRAM_BASE, SRAM_BYTES = 0x20000000, 0x4000  # firmware/hc32l170.ld RAM

PERI_CLKEN0, CLK_WDT = 0x40002020, 1 << 15
WDT_RST = 0x40000F80


def slot_problems(data):
    """The checks the MCU makes (frame.c slot_ok); [] if the image is a valid
    committed slot of exactly header + payload bytes."""
    if len(data) < 32 or data[:4] != FRAME_MAGIC:
        return [f"not a frame slot image (magic {data[:4]!r})"]
    ver, bpp, enc, _flags, _seq, _id, length, crc, commit = struct.unpack_from("<BBBBI8sIII", data, 4)
    return [m for bad, m in [
        (ver != 1, f"version {ver}"), (bpp not in (1, 2), f"bpp {bpp}"), (enc != 0, f"encoding {enc}"),
        (length != bpp * 12480, f"length {length}"), (len(data) != 32 + length, f"file size {len(data)}"),
        (len(data) > SLOT_BYTES, "too big"),
        (commit != 0, "not committed"),
        (zlib.crc32(data[32:32 + length]) != crc, "CRC mismatch")] if bad]


def check_slot(data):
    problems = slot_problems(data)
    if problems:
        sys.exit("frame slot rejected: " + ", ".join(problems))


def slot_seq(raw):
    """seq of a slot read from flash (trailing erased bytes allowed) if the MCU
    would accept it, else None."""
    if len(raw) < 32 or raw[:4] != FRAME_MAGIC:
        return None
    length = struct.unpack_from("<I", raw, 20)[0]
    if 32 + length > min(len(raw), SLOT_BYTES) or slot_problems(raw[:32 + length]):
        return None
    return struct.unpack_from("<I", raw, 8)[0]


def next_seq(raw_a, raw_b):
    """A seq that beats both slots (frame.c compares (int32)(b - a) > 0)."""
    a, b = slot_seq(raw_a), slot_seq(raw_b)
    if a is None and b is None:
        return 1
    best = a if b is None else b if a is None or (b - a) & 0xFFFFFFFF in range(1, 1 << 31) else a
    return (best + 1) & 0xFFFFFFFF


def with_seq(data, seq):
    """The slot image with another seq. The CRC covers the payload only, so
    only the header word changes."""
    return data[:8] + struct.pack("<I", seq) + data[12:]


def check_vectors(data, base, end, what):
    sp, reset = struct.unpack_from("<II", data, base)
    if not SRAM_BASE < sp <= SRAM_BASE + SRAM_BYTES:
        sys.exit(f"{what}: initial SP {sp:#x} is not in SRAM ({SRAM_BASE:#x}..{SRAM_BASE + SRAM_BYTES:#x})")
    if not reset & 1 or not base <= (reset & ~1) < end:
        sys.exit(f"{what}: reset vector {reset:#x} is not a Thumb address in {base:#x}..{end:#x}")


def check_firmware(data):
    """Sanity checks for a code image written at 0. With a bootloader
    (design 4.7: info block TH5B at 0xC0) the image must be th5-full.bin:
    the bootloader, then an app with TH5A at 0x10C0. An app linked for
    0x1000 on its own is refused: nothing would start it."""
    if not data:
        sys.exit("image is empty")
    if len(data) > CODE_LIMIT:
        sys.exit(f"image is {len(data)} B, over the {CODE_LIMIT:#x} code region")
    if len(data) < 8:
        sys.exit(f"image is {len(data)} B, too short for a vector table")
    if data[0xC0:0xC4] == b"TH5A":
        sys.exit("this is an app linked for 0x1000; write th5-full.bin (bootloader + app)")
    if data[0xC0:0xC4] == b"TH5B":
        if len(data) < 0x1100 or data[0x10C0:0x10C4] != b"TH5A":
            sys.exit("bootloader without an app at 0x1000 (TH5A at 0x10C0)")
        check_vectors(data, 0, 0x1000, "bootloader")
        check_vectors(data, 0x1000, CODE_LIMIT, "app")
        return
    check_vectors(data, 0, CODE_LIMIT, "image")


def find_pack(arg):
    """--pack, else a pack file next to the repo or in ~/.cache/hc32, else None:
    pyOCD then uses the pack installed with `pyocd pack install hc32l170faua`."""
    if arg:
        return arg
    for p in (ROOT, Path.home() / ".cache/hc32"):
        if (p / PACK_NAME).exists():
            return str(p / PACK_NAME)
    return None


def feeding(target, flash):
    """Wrap the algorithm call so the WDT is fed before every erase/program step."""
    orig = flash._call_function

    def call(*a, **kw):
        target.write32(PERI_CLKEN0, target.read32(PERI_CLKEN0) | CLK_WDT)
        target.write32(WDT_RST, 0x1E)
        target.write32(WDT_RST, 0xE1)
        return orig(*a, **kw)

    flash._call_function = call


def read_all(target, start=0, size=FLASH_SIZE):
    target.halt()
    return bytes(target.read_memory_block8(start, size))


def program(session, data, base=0):
    t = session.target
    t.reset_and_halt()
    feeding(t, t.memory_map.get_boot_memory().flash)
    from pyocd.flash.loader import FlashLoader
    loader = FlashLoader(session, chip_erase="sector", smart_flash=True)
    loader.add_data(base, data)
    t0 = time.time()
    loader.commit()
    print(f"programmed {len(data)} B at {base:#x} in {time.time() - t0:.1f} s")
    back = read_all(t, base, len(data))
    if back != data:
        bad = next(i for i in range(len(data)) if back[i] != data[i])
        sys.exit(f"VERIFY FAILED at {base + bad:#x}")
    print("verify OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["write", "frame", "restore", "read", "sector-test"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--pack")
    ap.add_argument("--halt", action="store_true", help="leave the core halted")
    ap.add_argument("--seq", type=lambda v: int(v, 0),
                    help="frame: seq to write (default: newest valid slot's + 1)")
    args = ap.parse_args()
    if args.cmd in ("write", "frame", "read", "restore") and not args.file:
        ap.error(f"{args.cmd} needs a file argument")
    if args.seq is not None and not 0 <= args.seq <= 0xFFFFFFFF:
        ap.error("--seq must fit 32 bits")
    if args.cmd == "write":                 # before touching the probe
        check_firmware(Path(args.file).read_bytes())
    if args.cmd == "restore":
        stock = Path(args.file).read_bytes()
        if len(stock) != FLASH_SIZE:
            sys.exit(f"{args.file} is {len(stock)} B, not a full {FLASH_SIZE} B flash image; refusing")
        check_vectors(stock, 0, FLASH_SIZE, "backup")
        stock_sha = hashlib.sha256(stock).hexdigest()
        print("backup", "is the known stock 1.0.5 image" if stock_sha == STOCK_SHA256
              else f"is not the known stock image (sha256 {stock_sha}); restoring it as it is")

    from pyocd.core.helpers import ConnectHelper
    from pyocd.flash.eraser import FlashEraser

    opts = {"connect_mode": "attach", "resume_on_disconnect": not args.halt}
    with ConnectHelper.session_with_chosen_probe(
            target_override="hc32l170faua", pack=find_pack(args.pack), options=opts) as session:
        t = session.target
        if args.cmd == "read":
            data = read_all(t)
            Path(args.file).write_bytes(data)
            print(f"read {len(data)} B, sha256 {hashlib.sha256(data).hexdigest()}")
        elif args.cmd == "sector-test":
            addr = 0x1D000
            t.reset_and_halt()
            if read_all(t, addr, SECTOR) != b"\xff" * SECTOR:
                sys.exit(f"{addr:#x} is not erased; refusing")
            pattern = bytes((i * 7 + 3) & 0xFF for i in range(SECTOR))
            program(session, pattern, addr)
            feeding(t, t.memory_map.get_boot_memory().flash)
            FlashEraser(session, FlashEraser.Mode.SECTOR).erase([f"{addr:#x}-{addr + SECTOR:#x}"])
            ok = read_all(t, addr, SECTOR) == b"\xff" * SECTOR
            print("erase back to 0xFF:", "OK" if ok else "FAILED")
        elif args.cmd == "write":
            program(session, Path(args.file).read_bytes(), 0)
            # A committed staged update not yet installed (design 4.7) would
            # replace what was just written at the next reset: drop it.
            hdr = bytes(t.read_memory_block8(SLOT_A, 32))
            if hdr[:4] == b"TH5U" and hdr[24:28] == bytes(4) and hdr[28:32] == b"\xff" * 4:
                feeding(t, t.memory_map.get_boot_memory().flash)
                FlashEraser(session, FlashEraser.Mode.SECTOR).erase([f"{SLOT_A:#x}-{SLOT_A + SECTOR:#x}"])
                print("erased a pending staged update at 0x10000")
        elif args.cmd == "frame":
            # Dev path until the CBU link exists (M3): a committed slot image
            # from ha/tools/th5_render.py, written straight into slot A.
            data = Path(args.file).read_bytes()
            check_slot(data)
            t.halt()
            seq = args.seq
            if seq is None:                  # beat whatever the MCU would show now
                seq = next_seq(bytes(t.read_memory_block8(SLOT_A, SLOT_BYTES)),
                               bytes(t.read_memory_block8(SLOT_B, SLOT_BYTES)))
            print(f"frame seq {seq}")
            program(session, with_seq(data, seq), SLOT_A)
        elif args.cmd == "restore":
            program(session, stock, 0)       # a full image, checked above
            digest = hashlib.sha256(read_all(t)).hexdigest()
            print(f"flash sha256 {digest}", "(matches the backup)" if digest == stock_sha else "MISMATCH")
        t.reset()


if __name__ == "__main__":
    main()
