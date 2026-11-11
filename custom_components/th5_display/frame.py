"""Stored frame format, shared with the MCU firmware (firmware/src/frame.h).

A slot is a 32-byte little-endian header followed by the payload. The payload
is plane0 then (for 2 bpp) plane1; each plane is 416 rows of 30 bytes in
panel order: row = landscape x (0 = left), byte k bit 7-b = landscape y 8k+b
(0 = top), 1 = white. Pixel level = 2*p0 + p1: 3 white, 2 light grey,
1 dark grey, 0 black. A 1 bpp frame is plane0 only, so plane0 alone must
read as a plain threshold (level >= 2 -> white).

Pure Python: stdlib + Pillow, no Home Assistant imports.
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass

from PIL import Image

# --- constants, as frame.h
FRAME_MAGIC = 0x46354854          # "TH5F" little-endian
FRAME_VERSION = 1
FRAME_HDR_BYTES = 32
FRAME_FLAG_BAND = 0x01            # forecast layout: MCU draws y 0..55 itself
FRAME_COMMITTED = 0x00000000
FRAME_UNCOMMITTED = 0xFFFFFFFF    # erased flash, still being written
FRAME_BAND_BYTES = 7              # landscape y 0..55 = bytes 0..6 of a row
FRAME_SLOT_A = 0x00010000
FRAME_SLOT_B = 0x00016200
FRAME_SLOT_BYTES = 0x6200         # 25,088

ENC_RAW = 0

W, H = 416, 240                   # landscape canvas
ROW_BYTES = H // 8                # 30
FRAME_PLANE_BYTES = W * ROW_BYTES  # 12,480
BAND_H = FRAME_BAND_BYTES * 8     # 56
RULE_Y = BAND_H + 1               # 2 px rule under the band (57, 58): forecast and picture

# The only grey values a frame can carry, indexed by level.
LEVELS = (0, 85, 170, 255)

# <IBBBB I 8s I I I : magic, version, bpp, encoding, flags, seq, frame_id,
# length, crc32, commit. frame_id is kept as raw bytes (= LE u64).
_HDR = struct.Struct("<IBBBBI8sIII")
assert _HDR.size == FRAME_HDR_BYTES


def _level_lut(plane: int) -> list[int]:
    """Grey value -> plane bit (0/255) for the nearest of the four levels."""
    out = []
    for v in range(256):
        lvl = min(range(4), key=lambda i: abs(LEVELS[i] - v))
        bit = (lvl >> 1) & 1 if plane == 0 else lvl & 1
        out.append(255 if bit else 0)
    return out


_LUT0, _LUT1 = _level_lut(0), _level_lut(1)


def _pack_plane(img: Image.Image, lut: list[int]) -> bytes:
    # Transpose to 240 wide x 416 tall: one image row = one panel row
    # (landscape x), and mode "1" packs it MSB first, 1 = white, which is
    # exactly byte k bit 7-b = y 8k+b.
    bits = img.point(lut).transpose(Image.Transpose.TRANSPOSE).convert("1", dither=Image.Dither.NONE)
    data = bits.tobytes()
    assert len(data) == FRAME_PLANE_BYTES
    return data


def encode_planes(img: Image.Image, bpp: int = 2) -> bytes:
    """416x240 'L' image -> payload (plane0 [+ plane1]).

    Values are snapped to the nearest of 0/85/170/255. For bpp=1 only plane0
    is emitted, which is the threshold level >= 2 (grey >= 170 -> white)."""
    if img.size != (W, H):
        raise ValueError(f"image must be {W}x{H}, got {img.size}")
    if bpp not in (1, 2):
        raise ValueError("bpp must be 1 or 2")
    img = img.convert("L")
    out = _pack_plane(img, _LUT0)
    if bpp == 2:
        out += _pack_plane(img, _LUT1)
    return out


def _unpack_plane(data: bytes) -> Image.Image:
    return Image.frombytes("1", (H, W), bytes(data)).transpose(Image.Transpose.TRANSPOSE).convert("L")


def decode_planes(payload: bytes, bpp: int) -> Image.Image:
    """Payload -> 416x240 'L' image with values from LEVELS."""
    if len(payload) != FRAME_PLANE_BYTES * bpp:
        raise ValueError(f"payload is {len(payload)} bytes, want {FRAME_PLANE_BYTES * bpp}")
    p0 = _unpack_plane(payload[:FRAME_PLANE_BYTES])          # 0/255
    if bpp == 1:
        return p0
    p1 = _unpack_plane(payload[FRAME_PLANE_BYTES:])
    # level = 2*p0 + p1, mapped to grey via LEVELS
    lvl = Image.merge("RGB", (p0, p1, p1)).convert("L", (2 / 3, 1 / 3, 0, 0))  # 0,85,170,255
    return lvl.point(lambda v: min(LEVELS, key=lambda q: abs(q - v)))


def frame_id_of(payload: bytes, bpp: int, flags: int = 0) -> int:
    """Default frame id: first 8 bytes of sha256(bpp, flags, payload) as LE u64,
    so the same planes with another layout flag are a different frame. Never 0
    (the MCU reports 0 for "no frame")."""
    h = hashlib.sha256(bytes((bpp, flags)) + payload)
    return int.from_bytes(h.digest()[:8], "little") or 1


@dataclass(frozen=True)
class SlotHeader:
    magic: int
    version: int
    bpp: int
    encoding: int
    flags: int
    seq: int
    frame_id: int
    length: int
    crc32: int
    commit: int

    @property
    def committed(self) -> bool:
        return self.commit == FRAME_COMMITTED


def build_slot(payload: bytes, bpp: int, flags: int = 0, seq: int = 1,
               frame_id: int | None = None, commit: bool = True) -> bytes:
    """Header + payload, ready to write at FRAME_SLOT_A or FRAME_SLOT_B.

    commit=True writes commit = 0 (pre-committed, as the SWD dev path does);
    commit=False leaves 0xFFFFFFFF for the MCU to clear after its CRC check."""
    if bpp not in (1, 2):
        raise ValueError("bpp must be 1 or 2")
    if len(payload) != FRAME_PLANE_BYTES * bpp:
        raise ValueError(f"payload is {len(payload)} bytes, want {FRAME_PLANE_BYTES * bpp}")
    if not 0 <= seq <= 0xFFFFFFFF or not 0 <= flags <= 0xFF:
        raise ValueError("seq or flags out of range")
    fid = frame_id_of(payload, bpp, flags) if frame_id is None else frame_id
    if not 0 < fid <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("frame_id must be 1..2^64-1")
    hdr = _HDR.pack(FRAME_MAGIC, FRAME_VERSION, bpp, ENC_RAW, flags, seq,
                    fid.to_bytes(8, "little"), len(payload),
                    zlib.crc32(payload) & 0xFFFFFFFF,
                    FRAME_COMMITTED if commit else FRAME_UNCOMMITTED)
    slot = hdr + payload
    assert len(slot) <= FRAME_SLOT_BYTES
    return slot


def parse_slot(data: bytes, require_commit: bool = True) -> tuple[SlotHeader, bytes]:
    """Validate a slot image (trailing erased bytes allowed) and return
    (header, payload). Raises ValueError on any mismatch."""
    if len(data) < FRAME_HDR_BYTES:
        raise ValueError("shorter than the header")
    magic, ver, bpp, enc, flags, seq, fid, length, crc, commit = _HDR.unpack_from(data)
    h = SlotHeader(magic, ver, bpp, enc, flags, seq, int.from_bytes(fid, "little"), length, crc, commit)
    if magic != FRAME_MAGIC:
        raise ValueError(f"bad magic 0x{magic:08x}")
    if ver != FRAME_VERSION:
        raise ValueError(f"unsupported version {ver}")
    if bpp not in (1, 2):
        raise ValueError(f"bad bpp {bpp}")
    if enc != ENC_RAW:
        raise ValueError(f"unsupported encoding {enc}")
    if length != FRAME_PLANE_BYTES * bpp:
        raise ValueError(f"length {length} does not match bpp {bpp}")
    if FRAME_HDR_BYTES + length > FRAME_SLOT_BYTES:
        raise ValueError("does not fit a slot")
    if len(data) < FRAME_HDR_BYTES + length:
        raise ValueError("truncated payload")
    payload = bytes(data[FRAME_HDR_BYTES:FRAME_HDR_BYTES + length])
    if zlib.crc32(payload) & 0xFFFFFFFF != crc:
        raise ValueError("CRC mismatch")
    if commit not in (FRAME_COMMITTED, FRAME_UNCOMMITTED):
        raise ValueError(f"bad commit word 0x{commit:08x}")
    if require_commit and commit != FRAME_COMMITTED:
        raise ValueError("slot not committed")
    return h, payload
