"""TIME DST data (ha/.../timeinfo.py, used by fakecbu), ImgBegin.from_slot
validation and the offline checks of tools/hc32_flash.py."""
import datetime as dt
import struct
from zoneinfo import ZoneInfo

import pytest

from custom_components.th5_display import frame
from custom_components.th5_display.timeinfo import time_fields
from tools import hc32_flash as F
from tools.th5link import proto as P


def utc(*a):
    return int(dt.datetime(*a, tzinfo=dt.timezone.utc).timestamp())


@pytest.mark.parametrize("zone,now,want", [
    ("Europe/Oslo", utc(2026, 11, 1, 8), (60, utc(2027, 3, 28, 1), 120)),
    ("Europe/Oslo", utc(2027, 1, 1), (60, utc(2027, 3, 28, 1), 120)),
    ("Europe/Oslo", utc(2026, 7, 1), (120, utc(2026, 10, 25, 1), 60)),
    ("America/New_York", utc(2026, 11, 1, 8), (-300, utc(2027, 3, 14, 7), -240)),
    ("Australia/Lord_Howe", utc(2026, 11, 1), (660, utc(2027, 4, 3, 15), 630)),
    ("Australia/Lord_Howe", utc(2027, 6, 1), (630, utc(2027, 10, 2, 15, 30), 660)),
    ("Asia/Kolkata", utc(2026, 11, 1), (330, 0, 0)),
    ("UTC", utc(2026, 11, 1), (0, 0, 0)),
])
def test_time_fields(zone, now, want):
    got = time_fields(ZoneInfo(zone), now)
    assert got == want
    t = P.Time(now, *got)
    assert t.check() is None
    if got[1]:
        assert t.next_change_utc > now


def test_time_fields_exact_second_and_fixed_offset():
    tz = ZoneInfo("Europe/Oslo")
    _, change, _ = time_fields(tz, utc(2026, 11, 1))
    before = dt.datetime.fromtimestamp(change - 1, tz).utcoffset()
    after = dt.datetime.fromtimestamp(change, tz).utcoffset()
    assert before != after
    assert time_fields(dt.timezone(dt.timedelta(hours=2)), utc(2026, 11, 1)) == (120, 0, 0)


# --------------------------------------------------------------------------- from_slot


def good_slot(**kw):
    return frame.build_slot(bytes(frame.FRAME_PLANE_BYTES), 1, **kw)


def test_from_slot_rejects_bad_slots():
    s = good_slot()
    b, payload = P.ImgBegin.from_slot(s)
    assert len(payload) == b.length
    with pytest.raises(ValueError, match="shorter"):
        P.ImgBegin.from_slot(s[:20])
    with pytest.raises(ValueError, match="mismatch"):
        P.ImgBegin.from_slot(s[:-1])
    with pytest.raises(ValueError, match="not committed"):
        P.ImgBegin.from_slot(good_slot(commit=False))
    big = bytearray(s)
    struct.pack_into("<I", big, 20, P.IMG_MAX + 1)
    with pytest.raises(ValueError, match="fit"):
        P.ImgBegin.from_slot(bytes(big))


# --------------------------------------------------------------------------- hc32_flash


def vectors(sp=0x20004000, reset=0x101, n=64):
    return struct.pack("<II", sp, reset) + bytes(n)


def test_check_firmware():
    F.check_firmware(vectors())
    F.check_firmware(vectors(sp=0x20000100, reset=0xFFFF))
    for bad in (b"", b"\x00" * 4, vectors(sp=0), vectors(sp=0x20004004), vectors(sp=0x20000000),
                vectors(reset=0x100), vectors(reset=0x10001), vectors(n=F.CODE_LIMIT)):
        with pytest.raises(SystemExit):
            F.check_firmware(bad)


def test_stock_hash_is_full():
    assert len(F.STOCK_SHA256) == 64 and F.STOCK_SHA256.startswith("63c59a16")


def test_slot_seq_selection_and_rewrite():
    a = good_slot(seq=7)
    b = good_slot(seq=9)
    pad = lambda s: s + b"\xff" * (F.SLOT_BYTES - len(s))  # noqa: E731
    blank = b"\xff" * F.SLOT_BYTES
    assert F.slot_seq(pad(a)) == 7 and F.slot_seq(blank) is None
    assert F.next_seq(pad(a), pad(b)) == 10 and F.next_seq(pad(b), pad(a)) == 10
    assert F.next_seq(blank, pad(a)) == 8 and F.next_seq(blank, blank) == 1
    # a corrupt slot does not count
    bad = bytearray(pad(b))
    bad[100] ^= 1
    assert F.next_seq(pad(a), bytes(bad)) == 8
    # wraparound: 0 beats 0xFFFFFFFF in the MCU's signed comparison
    assert F.next_seq(pad(good_slot(seq=0xFFFFFFFF)), pad(good_slot(seq=0))) == 1
    # only the header word changes; the CRC still holds
    out = F.with_seq(a, 1234)
    h, pl = frame.parse_slot(out)
    assert h.seq == 1234 and pl == frame.parse_slot(a)[1] and out[:8] == a[:8] and out[12:] == a[12:]
