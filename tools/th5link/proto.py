"""Frames, payloads and CRCs of the MCU <-> CBU link (docs/protocol.md).

Mirrors firmware/src/link_proto.h byte for byte. Stdlib only.

Frame:  A5 | ver | seq | cmd | len lo | len hi | payload[len] | crc lo | crc hi
CRC-16/CCITT-FALSE over ver..payload (the A5 is not covered), sent LE.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

SOF = 0xA5
VER = 1
HDR_BYTES = 6
CRC_BYTES = 2
OVERHEAD = HDR_BYTES + CRC_BYTES
MAX_TO_MCU = 1024          # CBU -> MCU payload cap
MAX_TO_CBU = 256           # MCU -> CBU payload cap

BAUD_SLOW = 115200
BAUD_FAST = 500000

# Timing (seconds)
T_ACK = 0.200
T_ACK_LONG = 1.000         # IMG_BEGIN, IMG_END, FW_BEGIN, FW_END
RETRIES = 3                # after the first attempt: 4 attempts in all
T_PROBE = 0.100            # baud probe window
T_PROBE_GAP = 0.002        # CBU: HELLO_RSP received -> PING at the new rate
T_SETTLE = 0.010           # CBU: revert to 115200 -> next byte
T_GARBAGE = 0.050          # garbage this long after the last valid frame -> revert
T_INTERBYTE = 0.020        # partial frame dropped after this much silence
T_HELLO = 5.0
T_CAP = 45.0
T_HOLD_MAX = 900.0
T_BYE_OFF = 0.050
T_BYE_TC_MAX = 0.100      # CLOSING ends this long after the BYE ACK at the latest
T_NET_GRACE = 2.0

# Commands
HELLO, PING, NET, TIME, CONFIG, IMG_BEGIN, IMG_DATA, IMG_END, HOLD, BYE = range(0x01, 0x0B)
FW_BEGIN, FW_END = 0x0B, 0x0C
FORGET = 0x40
ACK, HELLO_RSP, ERR = 0x80, 0x81, 0x82
RESPONSE_BIT = 0x80
CMD_NAMES = {HELLO: "HELLO", PING: "PING", NET: "NET", TIME: "TIME", CONFIG: "CONFIG",
             IMG_BEGIN: "IMG_BEGIN", IMG_DATA: "IMG_DATA", IMG_END: "IMG_END", HOLD: "HOLD",
             BYE: "BYE", FW_BEGIN: "FW_BEGIN", FW_END: "FW_END", FORGET: "FORGET", ACK: "ACK",
             HELLO_RSP: "HELLO_RSP", ERR: "ERR"}
CBU_REQUESTS = {HELLO, PING, NET, TIME, CONFIG, IMG_BEGIN, IMG_DATA, IMG_END, HOLD, BYE, FW_BEGIN, FW_END}
MCU_REQUESTS = {PING, FORGET}


def is_response(cmd: int) -> bool:
    return bool(cmd & RESPONSE_BIT)


def ack_timeout(cmd: int) -> float:
    return T_ACK_LONG if cmd in (IMG_BEGIN, IMG_END, FW_BEGIN, FW_END) else T_ACK


# Error codes
E_UNKNOWN_CMD, E_BAD_LEN, E_BAD_VALUE, E_VERSION, E_STATE, E_OFFSET, E_TOO_BIG, E_CRC, \
    E_FLASH, E_BUSY = range(1, 11)

# HELLO_RSP fields
RST_POR, RST_BOR, RST_LVD, RST_WDT, RST_PIN, RST_SOFT, RST_LOCKUP, RST_OTHER = (1 << i for i in range(8))
TRIG_TIMER, TRIG_BUTTON, TRIG_UNITS, TRIG_BOOT, TRIG_CHARGE, TRIG_BENCH, TRIG_RETRY = range(7)
F_TIME_VALID, F_FRAME_VALID, F_SENSOR_OK, F_FORGET_PENDING, F_BENCH, F_FACTORY_RESET = (1 << i for i in range(6))
# HELLO flags (byte 1)
HELLO_F_SLOW_ONLY = 0x01   # the CBU stays at 115200: the MCU offers no 500 k
CHG_DISCHARGING, CHG_CHARGING, CHG_FULL, CHG_UNKNOWN = range(4)
RES_NONE, RES_OK, RES_NO_HELLO, RES_TUYA, RES_NET_FAIL, RES_CAP, RES_LINK, RES_ABORT = range(8)
T_NONE = -0x8000
RH_NONE = 0xFFFF
PCT_NONE = 0xFF

# NET states (bit 7 = failure)
NET_WIFI_CONNECTING, NET_WIFI_UP, NET_API_UP, NET_HA_CONFIG, NET_OTA = range(1, 6)
NET_FAIL_WIFI, NET_FAIL_API, NET_FAIL_NO_HA, NET_FAIL_NO_CREDENTIALS = range(0x80, 0x84)

# BYE reasons
BYE_DONE, BYE_NO_HA, BYE_NET_FAIL, BYE_LINK, BYE_FORGOT = range(5)
BYE_F_HISTORY_ACK = 1

# IMG_BEGIN answers (ACK data byte)
IMG_START, IMG_HAVE = 0, 1
IMG_CHUNK = 1020           # data bytes per IMG_DATA (+4 offset = 1024 payload)
FRAME_PLANE_BYTES = 416 * 30
FRAME_SLOT_BYTES = 0x6200
FRAME_HDR_BYTES = 32
IMG_MAX = FRAME_SLOT_BYTES - FRAME_HDR_BYTES

# MCU firmware package (.fwu, firmware/src/fwu.h, design 4.7): 32 B staging header + image
FWU_MAGIC = 0x55354854     # "TH5U"
FWU_BOOT_MAGIC = 0x42354854  # "TH5B", the bootloader's info block at 0xC0
FWU_HDR_BYTES = 32
FWU_APP_BASE = 0x1000
FWU_MIN_IMAGE, FWU_MAX_IMAGE = 64, 50144
FWU_PKG_MAX = FWU_HDR_BYTES + FWU_MAX_IMAGE    # 50 176: the staging area
FWU_INFO_OFF = 0xC0
FWS_NO_BOOT, FWS_STAGED = 1, 2                  # STATE details for FW_BEGIN / IMG_BEGIN

# CONFIG tags: tag -> (name, struct fmt, min, max, default)
CFG_MODE, CFG_SYNC_MIN, CFG_CLOCK_MIN, CFG_CLOCK_24H, CFG_FULL_N, CFG_T_OFF, CFG_RH_OFF, CFG_FLAGS = range(1, 9)
CFG_FLAG_BADGE, CFG_FLAG_MAINTENANCE, CFG_FLAG_BAND_MONO, CFG_FLAG_BAND_SINGLE_RESET = 1, 2, 4, 8
MODE_PICTURE, MODE_FORECAST = 0, 1
CFG_TAGS = {
    CFG_MODE: ("mode", "<B", 0, 1, MODE_FORECAST),
    CFG_SYNC_MIN: ("sync_min", "<H", 15, 240, 60),
    CFG_CLOCK_MIN: ("clock_min", "<B", 1, 30, 1),
    CFG_CLOCK_24H: ("clock_24h", "<B", 0, 1, 1),
    CFG_FULL_N: ("full_n", "<B", 1, 255, 30),
    CFG_T_OFF: ("t_off", "<h", -1000, 1000, 0),
    CFG_RH_OFF: ("rh_off", "<h", -2000, 2000, 0),
    CFG_FLAGS: ("flags", "<B", 0, 15, 0),
}
CFG_DEFAULTS = {tag: spec[4] for tag, spec in CFG_TAGS.items()}

# SWD mailbox
MBOX_MAGIC = 0x4D354854    # "TH5M"
MBOX_VERSION = 1
MBOX_RING = 1536
MBOX_HDR_BYTES = 32
MBOX_BYTES = MBOX_HDR_BYTES + 2 * MBOX_RING
MBOX_OFF = dict(magic=0, version=4, ring_size=6, h2m_head=8, h2m_tail=12, m2h_head=16,
                m2h_tail=20, host_ctl=24, mcu_ctl=28, h2m_data=32, m2h_data=32 + MBOX_RING)
MBOX_HOST_BOOTED = 1       # host_ctl bit 0: the "CBU" is powered and booted
MBOX_MCU_CBU_ON = 1        # mcu_ctl bit 0: PB05 would be low
MBOX_MCU_FAST = 2          # mcu_ctl bit 1: the MCU is "at 500 kbaud" (informational)


# --- CRCs -------------------------------------------------------------------

def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xorout."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


# --- Frames -----------------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    seq: int
    cmd: int
    payload: bytes = b""
    ver: int = VER

    def encode(self) -> bytes:
        return encode_frame(self.seq, self.cmd, self.payload, self.ver)

    @property
    def name(self) -> str:
        return CMD_NAMES.get(self.cmd, f"0x{self.cmd:02X}")


def encode_frame(seq: int, cmd: int, payload: bytes = b"", ver: int = VER) -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too long")
    body = bytes([ver, seq & 0xFF, cmd]) + struct.pack("<H", len(payload)) + bytes(payload)
    return bytes([SOF]) + body + struct.pack("<H", crc16(body))


def decode_frame(buf: bytes, max_payload: int = 0xFFFF) -> Frame:
    """Decode exactly one complete frame or raise ValueError."""
    if len(buf) < OVERHEAD or buf[0] != SOF:
        raise ValueError("no frame")
    n = buf[4] | buf[5] << 8
    if n > max_payload or len(buf) != OVERHEAD + n:
        raise ValueError("bad length")
    if crc16(buf[1:HDR_BYTES + n]) != (buf[HDR_BYTES + n] | buf[HDR_BYTES + n + 1] << 8):
        raise ValueError("bad crc")
    return Frame(buf[2], buf[3], bytes(buf[HDR_BYTES:HDR_BYTES + n]), buf[1])


class StreamParser:
    """Byte stream -> frames, resynchronising after garbage (protocol.md 3.4).

    A candidate frame starts at an A5 whose ver is known and whose len is within
    max_payload. If the CRC fails, only that A5 is dropped and the scan restarts
    at the next byte, so a frame hidden behind a false start is still found.
    feed() returns the frames completed; `discarded` counts dropped bytes and
    `tuya` becomes True once a complete, checksum-valid Tuya MCU frame
    (55 AA ver cmd len_be16 data sum8) has been seen: a stock CBU."""

    def __init__(self, max_payload: int, versions=(VER,)):
        self.max_payload = max_payload
        self.versions = set(versions)
        self.buf = bytearray()
        self.discarded = 0
        self.tuya = False
        self.last_byte_t: float | None = None
        self._raw = bytearray()               # recent bytes, for Tuya detection

    def reset(self):
        self.discarded += len(self.buf)
        self.buf.clear()

    def feed(self, data: bytes, now: float | None = None) -> list[Frame]:
        if (now is not None and self.buf and self.last_byte_t is not None
                and now - self.last_byte_t > T_INTERBYTE):
            self._drop(1)                       # stale partial frame: drop its A5
            frames = self._scan()
        else:
            frames = []
        if not self.tuya and data:
            self._raw += data
            self.tuya = tuya_frame_in(self._raw)
            del self._raw[:-TUYA_WINDOW]
        if data and now is not None:
            self.last_byte_t = now
        self.buf += data
        return frames + self._scan()

    def _drop(self, n: int):
        del self.buf[:n]
        self.discarded += n

    def _scan(self) -> list[Frame]:
        out = []
        while self.buf:
            i = self.buf.find(SOF)
            if i < 0:
                self._drop(len(self.buf))
                break
            if i:
                self._drop(i)
            if len(self.buf) < HDR_BYTES:
                break
            n = self.buf[4] | self.buf[5] << 8
            if self.buf[1] not in self.versions or n > self.max_payload:
                self._drop(1)
                continue
            if len(self.buf) < OVERHEAD + n:
                break
            raw = bytes(self.buf[:OVERHEAD + n])
            try:
                f = decode_frame(raw, self.max_payload)
            except ValueError:
                self._drop(1)
                continue
            del self.buf[:OVERHEAD + n]
            out.append(f)
        return out


TUYA_WINDOW = 64 + 7      # longest Tuya frame we look for: 64 data bytes


def tuya_frame_in(buf: bytes) -> bool:
    """True if buf holds a complete Tuya MCU frame with a valid checksum, e.g.
    the stock CBU's product query 55 AA 00 01 00 00 00 (evidence/uart-capture).
    A bare 55 AA in boot noise does not count."""
    i = buf.find(b"\x55\xaa")
    while i >= 0:
        if i + 7 <= len(buf):
            n = buf[i + 4] << 8 | buf[i + 5]
            if n <= 64 and i + 7 + n <= len(buf) and buf[i + 2] in (0, 3) \
                    and sum(buf[i:i + 6 + n]) & 0xFF == buf[i + 6 + n]:
                return True
        i = buf.find(b"\x55\xaa", i + 1)
    return False


# --- Little-endian helpers ---------------------------------------------------

def u16(b, o): return b[o] | b[o + 1] << 8
def s16(b, o): return struct.unpack_from("<h", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]


def _need(payload: bytes, n: int, what: str):
    if len(payload) < n:
        raise ValueError(f"{what}: {len(payload)} < {n} bytes")


# --- Payloads ----------------------------------------------------------------
# Fixed layouts may grow at the end in later versions: parsers accept longer
# payloads and ignore the tail (protocol.md 4.0).

@dataclass
class Hello:
    proto_max: int = VER
    rx_max: int = MAX_TO_CBU
    fw_version: int = 0
    flags: int = 0                 # HELLO_F_*
    LEN = 8

    def encode(self) -> bytes:
        return struct.pack("<BBHI", self.proto_max, self.flags, self.rx_max, self.fw_version)

    @classmethod
    def parse(cls, p: bytes) -> "Hello":
        _need(p, cls.LEN, "HELLO")
        v, fl, rx, fw = struct.unpack_from("<BBHI", p)
        return cls(v, rx, fw, fl)


_HELLO_RSP = struct.Struct("<BBIHIQIBBHhHHBBIBBBB")


@dataclass(frozen=True)
class HistoryHour:
    hour_utc: int
    t_mean: int
    t_min: int
    t_max: int
    rh_mean: int
    rh_min: int
    rh_max: int
    samples: int

    FORMAT = struct.Struct("<IhhhHHHB")


@dataclass
class HelloRsp:
    proto: int = VER
    reset_cause: int = RST_POR
    baud: int = BAUD_FAST
    rx_max: int = MAX_TO_MCU
    fw_version: int = 0
    frame_id: int = 0
    settings_crc: int = 0
    units: int = 0                 # 0 degC, 1 degF
    trigger: int = TRIG_TIMER
    flags: int = 0
    t_centi: int = T_NONE          # 0.01 degC, offsets applied
    rh_centi: int = RH_NONE        # 0.01 %RH, offsets applied
    batt_mv: int = 0
    batt_pct: int = PCT_NONE
    charge: int = CHG_UNKNOWN
    last_session_ms: int = 0
    last_result: int = RES_NONE
    fail_count: int = 0
    boot_ver: int = 0              # 0 = no bootloader: FW_BEGIN is refused
    history: list[HistoryHour] | None = None  # None: old MCU without tail
    LEN = 44

    def encode(self) -> bytes:
        fixed = _HELLO_RSP.pack(self.proto, self.reset_cause, self.baud, self.rx_max, self.fw_version,
                               self.frame_id, self.settings_crc, self.units, self.trigger, self.flags,
                               self.t_centi, self.rh_centi, self.batt_mv, self.batt_pct, self.charge,
                               self.last_session_ms, self.last_result, self.fail_count, self.boot_ver, 0)
        if self.history is None:
            return fixed
        if len(self.history) > 8:
            raise ValueError("too many history hours")
        return fixed + bytes((len(self.history), 0)) + b"".join(HistoryHour.FORMAT.pack(
            h.hour_utc, h.t_mean, h.t_min, h.t_max, h.rh_mean, h.rh_min, h.rh_max, h.samples
        ) for h in self.history)

    @classmethod
    def parse(cls, p: bytes) -> "HelloRsp":
        _need(p, cls.LEN, "HELLO_RSP")
        v = _HELLO_RSP.unpack_from(p)
        history = None
        if len(p) > cls.LEN:
            _need(p, cls.LEN + 2, "HELLO_RSP history header")
            count, flags = p[cls.LEN:cls.LEN + 2]
            if count > 8 or flags != 0:
                raise ValueError("invalid HELLO_RSP history header")
            _need(p, cls.LEN + 2 + count * HistoryHour.FORMAT.size, "HELLO_RSP history")
            history = [HistoryHour(*HistoryHour.FORMAT.unpack_from(p, cls.LEN + 2 + i * 17))
                       for i in range(count)]
        return cls(*v[:-1], history=history)


assert _HELLO_RSP.size == HelloRsp.LEN


@dataclass
class Net:
    state: int
    detail: int = 0
    rssi: int = 0                  # dBm, 0 = unknown
    LEN = 3

    def encode(self) -> bytes:
        return struct.pack("<BBb", self.state, self.detail, self.rssi)

    @classmethod
    def parse(cls, p: bytes) -> "Net":
        _need(p, cls.LEN, "NET")
        return cls(*struct.unpack_from("<BBb", p))

    @property
    def failed(self) -> bool:
        return bool(self.state & 0x80)


@dataclass
class Time:
    utc: int
    offset_min: int = 0
    next_change_utc: int = 0       # 0 = no transition known
    next_offset_min: int = 0
    LEN = 12
    UTC_MIN = 1735689600           # 2025-01-01T00:00:00Z
    UTC_MAX = 4102444800           # 2100-01-01T00:00:00Z: the RTC year is 2 BCD digits

    def encode(self) -> bytes:
        return struct.pack("<IhIh", self.utc, self.offset_min, self.next_change_utc, self.next_offset_min)

    @classmethod
    def parse(cls, p: bytes) -> "Time":
        _need(p, cls.LEN, "TIME")
        return cls(*struct.unpack_from("<IhIh", p))

    def check(self) -> int | None:
        """None if valid, else the byte offset of the first bad field."""
        if self.utc < self.UTC_MIN:
            return 0
        if abs(self.offset_min) > 18 * 60:
            return 4
        if self.utc + self.offset_min * 60 >= self.UTC_MAX:
            return 0
        if self.next_change_utc >= self.UTC_MAX:
            return 6
        if self.next_change_utc and self.next_change_utc <= self.utc:
            return 6
        if abs(self.next_offset_min) > 18 * 60:
            return 10
        return None


def config_encode(values: dict[int, int]) -> bytes:
    """TLV: tag u8, len u8, value. Tags in ascending order (canonical form)."""
    out = bytearray()
    for tag in sorted(values):
        fmt = CFG_TAGS[tag][1] if tag in CFG_TAGS else None
        v = values[tag]
        raw = struct.pack(fmt, v) if fmt else bytes(v)
        out += bytes([tag, len(raw)]) + raw
    return bytes(out)


def config_parse(p: bytes) -> tuple[dict[int, int], int | None]:
    """-> (known tag values, offset of the first bad TLV or None). Unknown tags
    are skipped. Range is not checked here (see config_check)."""
    vals: dict[int, int] = {}
    i = 0
    while i < len(p):
        if i + 2 > len(p) or i + 2 + p[i + 1] > len(p):
            return vals, i
        tag, n = p[i], p[i + 1]
        if tag in CFG_TAGS:
            fmt = CFG_TAGS[tag][1]
            if n != struct.calcsize(fmt):
                return vals, i
            vals[tag] = struct.unpack_from(fmt, p, i + 2)[0]
        i += 2 + n
    return vals, None


def config_check(vals: dict[int, int]) -> int | None:
    """None if every value is in range, else the first bad tag."""
    for tag in sorted(vals):
        _n, _f, lo, hi, _d = CFG_TAGS[tag]
        if not lo <= vals[tag] <= hi:
            return tag
    return None


def settings_crc(values: dict[int, int]) -> int:
    """CRC-32 (zlib) of the canonical CONFIG encoding of every v1 tag."""
    full = {**CFG_DEFAULTS, **{k: v for k, v in values.items() if k in CFG_TAGS}}
    return crc32(config_encode(full))


@dataclass
class ImgBegin:
    frame_id: int
    bpp: int
    encoding: int
    flags: int
    length: int
    crc32: int
    LEN = 20

    def encode(self) -> bytes:
        return struct.pack("<QBBBBII", self.frame_id, self.bpp, self.encoding, self.flags, 0,
                           self.length, self.crc32)

    @classmethod
    def parse(cls, p: bytes) -> "ImgBegin":
        _need(p, cls.LEN, "IMG_BEGIN")
        fid, bpp, enc, fl, _r, n, c = struct.unpack_from("<QBBBBII", p)
        return cls(fid, bpp, enc, fl, n, c)

    @classmethod
    def from_slot(cls, slot: bytes) -> tuple["ImgBegin", bytes]:
        """Parse a stored-slot image (firmware/src/frame.h) into the BEGIN fields
        and the payload that IMG_DATA carries. ValueError unless it is a whole,
        committed slot image that fits a slot."""
        if len(slot) < FRAME_HDR_BYTES:
            raise ValueError("slot image shorter than the header")
        magic, ver, bpp, enc, flags, _seq, fid, length, crc, commit = struct.unpack_from(
            "<IBBBBIQIII", slot)
        if magic != 0x46354854 or ver != 1:
            raise ValueError("not a TH5F slot image")
        if commit != 0:
            raise ValueError("slot image not committed")
        if length > IMG_MAX:
            raise ValueError(f"payload length {length} does not fit a slot")
        payload = slot[FRAME_HDR_BYTES:FRAME_HDR_BYTES + length]
        if len(payload) != length or crc32(payload) != crc:
            raise ValueError("slot payload length or CRC mismatch")
        return cls(fid, bpp, enc, flags, length, crc), payload


def img_data_encode(offset: int, data: bytes) -> bytes:
    return struct.pack("<I", offset) + bytes(data)


def img_data_parse(p: bytes) -> tuple[int, bytes]:
    _need(p, 5, "IMG_DATA")
    return u32(p, 0), bytes(p[4:])


def img_end_encode(frame_id: int) -> bytes:
    return struct.pack("<Q", frame_id)


def img_end_parse(p: bytes) -> int:
    _need(p, 8, "IMG_END")
    return u64(p, 0)


@dataclass
class FwBegin:
    """FW_BEGIN (4.14): the package header's fields."""
    fw_version: int
    length: int
    crc32: int
    load: int = FWU_APP_BASE
    LEN = 16

    def encode(self) -> bytes:
        return struct.pack("<4I", self.fw_version, self.length, self.crc32, self.load)

    @classmethod
    def parse(cls, p: bytes) -> "FwBegin":
        _need(p, cls.LEN, "FW_BEGIN")
        return cls(*struct.unpack_from("<4I", p))


def fwu_package(image: bytes, fw_version: int, min_boot_ver: int = 1) -> bytes:
    """A .fwu package as firmware/tools/mkfwu.py writes it (commit/done erased)."""
    return struct.pack("<8I", FWU_MAGIC, fw_version, len(image), crc32(image), FWU_APP_BASE,
                       min_boot_ver, 0xFFFFFFFF, 0xFFFFFFFF) + bytes(image)


def fwu_parse(pkg: bytes) -> tuple[FwBegin, bytes, int]:
    """.fwu package -> (FW_BEGIN fields, image, min_boot_ver). ValueError unless
    the header and the image CRC check out."""
    if len(pkg) < FWU_HDR_BYTES:
        raise ValueError("package shorter than the header")
    magic, ver, length, crc, load, min_boot, _c, _d = struct.unpack_from("<8I", pkg)
    if magic != FWU_MAGIC:
        raise ValueError("not a TH5U package")
    if load != FWU_APP_BASE:
        raise ValueError(f"load address {load:#x}")
    if not FWU_MIN_IMAGE <= length <= FWU_MAX_IMAGE or length % 4 or len(pkg) != FWU_HDR_BYTES + length:
        raise ValueError(f"bad length {length}")
    image = bytes(pkg[FWU_HDR_BYTES:])
    if crc32(image) != crc:
        raise ValueError("image CRC mismatch")
    return FwBegin(ver, length, crc, load), image, min_boot


def fw_end_encode(crc: int) -> bytes:
    return struct.pack("<I", crc)


def fw_end_parse(p: bytes) -> int:
    _need(p, 4, "FW_END")
    return u32(p, 0)


def hold_encode(seconds: int) -> bytes:
    return struct.pack("<H", seconds)


def hold_parse(p: bytes) -> int:
    _need(p, 2, "HOLD")
    return u16(p, 0)


def bye_encode(reason: int = BYE_DONE, history_ack: bool = False) -> bytes:
    return bytes([reason, BYE_F_HISTORY_ACK]) if history_ack else bytes([reason])


def bye_parse(p: bytes) -> int:
    _need(p, 1, "BYE")
    return p[0]


def ack_encode(cmd: int, data: bytes = b"") -> bytes:
    return bytes([cmd]) + bytes(data)


def ack_parse(p: bytes) -> tuple[int, bytes]:
    _need(p, 1, "ACK")
    return p[0], bytes(p[1:])


@dataclass
class Err:
    cmd: int
    code: int
    detail: int = 0
    LEN = 6

    def encode(self) -> bytes:
        return struct.pack("<BBI", self.cmd, self.code, self.detail)

    @classmethod
    def parse(cls, p: bytes) -> "Err":
        _need(p, cls.LEN, "ERR")
        return cls(*struct.unpack_from("<BBI", p))


# Minimum payload length per request (longer is accepted, protocol.md 4.0).
MIN_LEN = {HELLO: Hello.LEN, PING: 0, NET: Net.LEN, TIME: Time.LEN, CONFIG: 0,
           IMG_BEGIN: ImgBegin.LEN, IMG_DATA: 5, IMG_END: 8, HOLD: 2, BYE: 1, FORGET: 0,
           FW_BEGIN: FwBegin.LEN, FW_END: 4}
PING_MAX = 16


def describe(f: Frame) -> str:
    """One-line human description, for logs and the worked example."""
    p = f.payload
    try:
        if f.cmd == HELLO:
            d = Hello.parse(p)
        elif f.cmd == HELLO_RSP:
            d = HelloRsp.parse(p)
        elif f.cmd == NET:
            d = Net.parse(p)
        elif f.cmd == TIME:
            d = Time.parse(p)
        elif f.cmd == CONFIG:
            d = {CFG_TAGS[k][0]: v for k, v in config_parse(p)[0].items()}
        elif f.cmd == IMG_BEGIN:
            d = ImgBegin.parse(p)
        elif f.cmd == IMG_DATA:
            off, data = img_data_parse(p)
            d = f"offset={off} n={len(data)}"
        elif f.cmd == IMG_END:
            d = f"frame_id={img_end_parse(p):016x}"
        elif f.cmd == FW_BEGIN:
            d = FwBegin.parse(p)
        elif f.cmd == FW_END:
            d = f"crc32={fw_end_parse(p):08x}"
        elif f.cmd == ERR:
            d = Err.parse(p)
        elif f.cmd == ACK:
            c, data = ack_parse(p)
            d = f"{CMD_NAMES.get(c, hex(c))} {data.hex(' ')}".rstrip()
        else:
            d = p.hex(" ")
    except ValueError as e:
        d = f"<{e}>"
    return f"{f.name} seq={f.seq} {d}"


@dataclass
class Settings:
    """The MCU's stored settings (v1 tags), with defaults."""
    values: dict = field(default_factory=lambda: dict(CFG_DEFAULTS))

    @property
    def crc(self) -> int:
        return settings_crc(self.values)
