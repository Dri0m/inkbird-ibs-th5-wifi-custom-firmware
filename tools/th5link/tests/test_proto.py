"""Frames, CRC, stream resync and payload round trips.

    uv run --with pytest pytest tools/th5link/tests
"""
import struct
from pathlib import Path

import pytest

from tools.th5link import proto as P

ROOT = Path(__file__).resolve().parents[3]
SLOT = ROOT / "ha/out/frame-C.slot"


@pytest.mark.parametrize("data,crc", [
    (b"123456789", 0x29B1),          # CRC-16/CCITT-FALSE check value
    (b"", 0xFFFF),
    (b"\x00", 0xE1F0),
    (b"A", 0xB915),
    (bytes(range(256)), None),
])
def test_crc16_vectors(data, crc):
    got = P.crc16(data)
    if crc is not None:
        assert got == crc
    # nibble-table form (as in link_proto.h) agrees with the bitwise form
    t = [0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
         0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF]
    for i in range(16):                      # each entry: nibble i at the top, 4 shifts
        c = i << 12
        for _ in range(4):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        assert t[i] == c
    c = 0xFFFF
    for b in data:
        c = ((c << 4) & 0xFFFF) ^ t[(c >> 12) ^ (b >> 4)]
        c = ((c << 4) & 0xFFFF) ^ t[(c >> 12) ^ (b & 15)]
    assert c == got


def test_frame_layout():
    raw = P.encode_frame(0x12, P.PING, b"\x01\x02")
    assert raw[:6] == bytes([0xA5, 1, 0x12, 0x02, 2, 0])
    assert raw[6:8] == b"\x01\x02"
    assert struct.unpack("<H", raw[8:])[0] == P.crc16(raw[1:8])   # not the A5
    assert len(raw) == P.OVERHEAD + 2


@pytest.mark.parametrize("n", [0, 1, 255, 256, 1024])
def test_frame_round_trip(n):
    payload = bytes((i * 31) & 0xFF for i in range(n))
    f = P.decode_frame(P.encode_frame(200, P.IMG_DATA, payload), P.MAX_TO_MCU)
    assert (f.seq, f.cmd, f.payload, f.ver) == (200, P.IMG_DATA, payload, 1)


def test_decode_rejects():
    raw = bytearray(P.encode_frame(1, P.BYE, b"\x00"))
    with pytest.raises(ValueError):
        P.decode_frame(bytes(raw), max_payload=0)
    raw[6] ^= 1
    with pytest.raises(ValueError):
        P.decode_frame(bytes(raw))


def test_stream_resync_over_boot_text_and_garbage():
    f1 = P.encode_frame(0, P.HELLO, P.Hello().encode())
    f2 = P.encode_frame(1, P.PING, b"\xA5\xA5")
    junk = (b"\r\nV:BK7231N_1.0.1\r\nlt_main()\r\n" + bytes([0xA5, 0x01, 0x00, 0x01, 0xFF, 0xFF])
            + bytes([0xA5, 0x07]) + b"\x00\xff\xa5")
    s = P.StreamParser(P.MAX_TO_MCU)
    got = s.feed(junk + f1 + b"\x13\x37" + f2)
    assert [(f.cmd, f.seq) for f in got] == [(P.HELLO, 0), (P.PING, 1)]
    assert s.discarded == len(junk) + 2
    assert not s.tuya


def test_stream_false_start_hiding_a_frame():
    """A5 followed by a plausible header whose 'payload' swallows a real frame:
    the CRC fails, only that A5 is dropped, and the real frame is found."""
    real = P.encode_frame(5, P.NET, P.Net(P.NET_API_UP).encode())
    fake_hdr = bytes([0xA5, 1, 9, 3, len(real) + 2, 0])
    s = P.StreamParser(P.MAX_TO_MCU)
    got = s.feed(fake_hdr + real + b"\x00\x00\x00\x00")
    assert [f.seq for f in got] == [5]


def test_stream_byte_at_a_time_and_split():
    frames = [P.encode_frame(i, P.PING, bytes([i] * i)) for i in range(10)]
    stream = b"".join(frames)
    s = P.StreamParser(P.MAX_TO_MCU)
    got = []
    for i in range(len(stream)):
        got += s.feed(stream[i:i + 1])
    assert [f.seq for f in got] == list(range(10))


def test_stream_interbyte_timeout_drops_partial():
    full = P.encode_frame(3, P.BYE, b"\x00")
    s = P.StreamParser(P.MAX_TO_MCU)
    assert s.feed(full[:5], now=0.0) == []
    got = s.feed(P.encode_frame(4, P.BYE, b"\x00"), now=0.5)   # 500 ms later
    assert [f.seq for f in got] == [4]


def test_stream_oversize_len_is_not_a_frame():
    s = P.StreamParser(P.MAX_TO_CBU)
    big = P.encode_frame(1, P.ACK, bytes(300))
    ok = P.encode_frame(2, P.ACK, b"\x02")
    assert [f.seq for f in s.feed(big + ok)] == [2]


def test_tuya_detection():
    s = P.StreamParser(P.MAX_TO_MCU)
    s.feed(b"\x00\x00boot\x55\xaa\x00\x01\x00\x00\x01")   # bad checksum: not Tuya
    assert not s.tuya
    for b in bytes.fromhex("55aa0001000000"):         # stock CBU product query (sniffed)
        s.feed(bytes([b]))
    assert s.tuya


def test_payload_round_trips():
    h = P.Hello(1, 256, 0x01020304)
    assert P.Hello.parse(h.encode()) == h and len(h.encode()) == P.Hello.LEN
    r = P.HelloRsp(1, P.RST_WDT | P.RST_POR, P.BAUD_FAST, 1024, 0x10000, 0x1122334455667788,
                   0xDEADBEEF, 1, P.TRIG_UNITS, P.F_TIME_VALID | P.F_BENCH, -1234, 5280, 3980,
                   78, P.CHG_FULL, 4321, P.RES_CAP, 3)
    assert len(r.encode()) == P.HelloRsp.LEN == 44
    assert P.HelloRsp.parse(r.encode()) == r
    r.history = []
    assert P.HelloRsp.parse(r.encode() + b"future") == r          # grows after known tail
    n = P.Net(P.NET_FAIL_WIFI, 15, -80)
    assert P.Net.parse(n.encode()) == n and n.failed
    t = P.Time(1793520000, 60, 1806195600, 120)
    assert P.Time.parse(t.encode()) == t and t.check() is None
    assert P.Time(1000, 0).check() == 0
    assert P.Time(1793520000, 0, 1793520000 - 1, 0).check() == 6
    b = P.ImgBegin(0x0123456789ABCDEF, 2, 0, 1, 24960, 0xCAFEBABE)
    assert P.ImgBegin.parse(b.encode()) == b and len(b.encode()) == P.ImgBegin.LEN
    assert P.img_data_parse(P.img_data_encode(1020, b"\x01\x02\x03\x04")) == (1020, b"\x01\x02\x03\x04")
    assert P.img_end_parse(P.img_end_encode(0xFFEEDDCCBBAA9988)) == 0xFFEEDDCCBBAA9988
    assert P.hold_parse(P.hold_encode(900)) == 900
    assert P.bye_parse(P.bye_encode(P.BYE_NO_HA)) == P.BYE_NO_HA
    assert P.ack_parse(P.ack_encode(P.PING, b"xy")) == (P.PING, b"xy")
    e = P.Err(P.IMG_DATA, P.E_OFFSET, 2040)
    assert P.Err.parse(e.encode()) == e


def test_config_tlv():
    vals = {P.CFG_MODE: 0, P.CFG_SYNC_MIN: 30, P.CFG_T_OFF: -50, P.CFG_RH_OFF: 120, P.CFG_FLAGS: 3}
    raw = P.config_encode(vals)
    assert raw[:3] == bytes([P.CFG_MODE, 1, 0])
    assert P.config_parse(raw) == (vals, None)
    # unknown tags (also tag 0x7F with a long value) are skipped
    raw2 = bytes([0x7F, 3, 9, 9, 9]) + raw + bytes([0x30, 0])
    assert P.config_parse(raw2) == (vals, None)
    # a known tag with the wrong length is an error at its offset
    assert P.config_parse(bytes([P.CFG_MODE, 2, 0, 0]))[1] == 0
    # truncated TLV
    assert P.config_parse(raw + bytes([P.CFG_FLAGS, 1]))[1] == len(raw)
    assert P.config_check({P.CFG_SYNC_MIN: 5}) == P.CFG_SYNC_MIN
    assert P.config_check(vals) is None


def test_settings_crc_canonical():
    # missing tags count as defaults; order and unknown tags do not matter
    assert P.settings_crc({}) == P.settings_crc(dict(P.CFG_DEFAULTS))
    assert P.settings_crc({P.CFG_SYNC_MIN: 60}) == P.settings_crc({})
    assert P.settings_crc({P.CFG_SYNC_MIN: 30}) != P.settings_crc({})
    assert P.settings_crc({}) == P.crc32(P.config_encode(P.CFG_DEFAULTS))


def test_img_begin_from_slot_file():
    slot = SLOT.read_bytes()
    b, payload = P.ImgBegin.from_slot(slot)
    assert b.length == len(payload) == b.bpp * P.FRAME_PLANE_BYTES
    assert P.crc32(payload) == b.crc32 and b.frame_id != 0
    assert len(payload) <= P.IMG_MAX
    assert -(-len(payload) // P.IMG_CHUNK) == 25          # 2 bpp: 25 chunks


def test_max_frames_fit_buffers():
    assert 4 + P.IMG_CHUNK == P.MAX_TO_MCU
    assert P.HelloRsp.LEN <= P.MAX_TO_CBU
    assert P.OVERHEAD + P.MAX_TO_MCU < P.MBOX_RING
