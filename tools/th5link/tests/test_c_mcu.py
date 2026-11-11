"""The C link engine (firmware/src/link.c + settings.c + frame.c, built for the
host with firmware/test/link_host.c) against the Python fake CBU, in the same
virtual-time loopback (sim.py) that checks the reference model mcumodel.py.

Each CMcu loads its own copy of the shared library, so every test starts with
fresh static state."""
import ctypes
import itertools
import re
import shutil
import struct
import subprocess
import types
from pathlib import Path

import pytest

from tools.th5link import proto as P
from tools.th5link.fakecbu import CbuScript, FakeCbu
from tools.th5link.sim import Sim, frames

ROOT = Path(__file__).resolve().parents[3]
FW = ROOT / "firmware"
SLOT = (ROOT / "ha/out/frame-C.slot").read_bytes()
BEGIN, PAYLOAD = P.ImgBegin.from_slot(SLOT)
SLOT_A, SLOT_B = 0x10000, 0x16200
LINK_OK, LINK_ERR = 1, 2

pytestmark = pytest.mark.skipif(not shutil.which("clang"), reason="clang not found")
_n = itertools.count()


@pytest.fixture(scope="module")
def libpath(tmp_path_factory):
    out = tmp_path_factory.mktemp("clink") / "liblink.so"
    srcs = [FW / "test/link_host.c"] + [FW / "src" / f for f in ("link.c", "settings.c", "frame.c", "history.c")]
    subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wconversion",
                    "-Wno-sign-conversion", "-shared", "-fPIC", "-DLINK_HOST",
                    "-fsanitize=undefined", f"-I{FW / 'src'}", *map(str, srcs), "-o", str(out)],
                   check=True)
    return out


class Outcome(ctypes.Structure):
    _fields_ = [("result", ctypes.c_uint8), ("icon", ctypes.c_uint8), ("status", ctypes.c_uint8),
                ("fail_count", ctypes.c_uint8), ("nohello_count", ctypes.c_uint8),
                ("committed", ctypes.c_bool), ("fw_staged", ctypes.c_bool),
                ("next_s", ctypes.c_uint32), ("session_ms", ctypes.c_uint32),
                ("sessions", ctypes.c_uint32)]


class Stats(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "discarded", "dup_requests", "reverts", "erases", "programs", "settings_writes")]


def ms(t: float) -> int:
    return int(round(t * 1000))


class CMcu:
    """The runner interface of mcumodel.McuModel, over the C engine."""

    def __init__(self, libpath: Path, erased=True, slot_a: bytes | None = None):
        copy = libpath.with_name(f"liblink{next(_n)}.so")
        shutil.copy(libpath, copy)
        L = self.lib = ctypes.CDLL(str(copy))
        L.h_outcome.restype = ctypes.POINTER(Outcome)
        L.h_stats.restype = ctypes.POINTER(Stats)
        L.h_frame_id.restype = ctypes.c_uint64
        L.h_setting.restype = ctypes.c_int32
        for f in ("h_next_deadline", "h_baud", "h_settings_crc", "h_take_tx", "h_frame_addr",
                  "h_settings_sector", "h_settings_seq", "h_next_after", "h_days", "h_set_units",
                  "h_units"):
            getattr(L, f).restype = ctypes.c_uint32
        self.flash = (ctypes.c_uint8 * 0x20000).in_dll(L, "host_flash")
        L.h_init(P.RST_POR, 1 if erased else 0)
        if slot_a:
            L.h_load(SLOT_A, slot_a, len(slot_a))
            L.h_reload()
        self.ep = types.SimpleNamespace(trace=None)
        self.now = 0.0
        self.batt_mv, self.charge = 3980, P.CHG_DISCHARGING
        self._rxp = P.StreamParser(P.MAX_TO_MCU)
        self._txp = P.StreamParser(P.MAX_TO_CBU)

    # --- runner interface ---
    @property
    def cbu_on(self):
        return bool(self.lib.h_active())

    @property
    def baud(self):
        return self.lib.h_baud()

    @property
    def outcome(self) -> Outcome:
        return self.lib.h_outcome().contents

    @property
    def stats(self) -> Stats:
        return self.lib.h_stats().contents

    @property
    def result(self):
        o = self.outcome
        return o.result if o.sessions else None

    def start_session(self, now, trigger=P.TRIG_TIMER, bench=False):
        self.now = now
        self._rxp, self._txp = P.StreamParser(P.MAX_TO_MCU), P.StreamParser(P.MAX_TO_CBU)
        return bool(self.lib.h_start(ms(now), trigger, int(bench), self.batt_mv, self.charge))

    def feed(self, data, now):
        self.now = now
        if self.ep.trace:
            for f in self._rxp.feed(data):
                self.ep.trace("rx", f, now)
        buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        self.lib.h_feed(buf, len(data), ms(now))

    def take_tx(self):
        out = (ctypes.c_uint8 * 16384)()
        n = self.lib.h_take_tx(out, 16384)
        data = bytes(out[:n])
        if self.ep.trace and data:
            for f in self._txp.feed(data):
                self.ep.trace("tx", f, self.now)
        return data

    def tx_done(self, now):
        self.now = now
        self.lib.h_tx_done(ms(now))

    def poll(self, now):
        self.now = now
        self.lib.h_poll(ms(now))

    def next_deadline(self):
        if not self.cbu_on:
            return None
        return self.lib.h_next_deadline(ms(self.now)) / 1000.0

    # --- inspection ---
    def mem(self, addr, n):
        return bytes(self.flash[addr:addr + n])

    @property
    def frame_id(self):
        return self.lib.h_frame_id()

    def frame_addr(self):
        return self.lib.h_frame_addr() if self.frame_id else None

    def setting(self, tag):
        return self.lib.h_setting(tag)

    def rtc(self):
        out = (ctypes.c_uint8 * 7)()
        self.lib.h_rtc_get(out)
        return tuple(out)


def make(libpath, fault=None, mcu=None, cbu=FakeCbu, **kw):
    mcu = mcu or CMcu(libpath)
    return Sim(mcu, lambda: cbu(CbuScript(**kw)), fault=fault)


def drop_nth(direction, cmd, n=1, acked=None):
    seen = [0]

    def fault(d, data, now):
        if d != direction:
            return data
        try:
            f = P.decode_frame(data)
        except ValueError:
            return data
        if f.cmd != cmd or (acked is not None and f.payload[:1] != bytes([acked])):
            return data
        seen[0] += 1
        return None if seen[0] == n else data
    return fault


# --- sessions ------------------------------------------------------------------

def test_full_session_into_slot_b(libpath):
    """Slot A holds an older frame: the new one goes to B with seq + 1."""
    old = bytearray(SLOT)
    struct.pack_into("<Q", old, 12, 0x1111)               # a different frame id
    mcu = CMcu(libpath, erased=False, slot_a=bytes(old))
    assert mcu.frame_addr() == SLOT_A
    sim = make(libpath, mcu=mcu, slot=SLOT,
               config={**CbuScript().config, P.CFG_SYNC_MIN: 30})
    assert sim.run_session(P.TRIG_BUTTON) == P.RES_OK
    cbu = sim.cbus[0]
    assert cbu.error is None and cbu.img_result == "committed"
    assert cbu.hello_rsp.frame_id == 0x1111 and cbu.hello_rsp.trigger == P.TRIG_BUTTON
    assert cbu.hello_rsp.settings_crc == 0xFAFE77C2            # defaults, protocol.md 4.6
    assert mcu.frame_id == BEGIN.frame_id and mcu.frame_addr() == SLOT_B
    assert mcu.mem(SLOT_B + 32, len(PAYLOAD)) == PAYLOAD
    hdr = mcu.mem(SLOT_B, 32)
    assert struct.unpack_from("<I", hdr, 8)[0] == 2 and hdr[28:32] == bytes(4)
    o = mcu.outcome
    assert (o.icon, o.committed, o.next_s) == (LINK_OK, True, 30 * 60)
    assert mcu.stats.settings_writes == 1
    assert mcu.setting(P.CFG_SYNC_MIN) == 30
    # TIME: utc0 + ~1.8 s at +60 min -> 2026-11-01 09:00:0x local, a Sunday
    y, mo, d, wd, h, mi, s = mcu.rtc()
    assert (y, mo, d, wd, h, mi) == (26, 11, 1, 0, 9, 0)
    assert cbu.ep.stats.reverts == 0 and mcu.stats.reverts == 0          # ran at 500 k
    rx = frames(sim, "mcu-rx")
    assert sum(f.cmd == P.IMG_DATA for f in rx) == 25
    assert mcu.stats.erases == 49                     # slot B was not blank: every sector
    t_bye = max(t for t, d, f in sim.trace if d == "mcu-tx" and f.cmd == P.ACK
                and f.payload[:1] == bytes([P.BYE]))
    assert sim.now - t_bye == pytest.approx(0.050, abs=0.002)
    assert o.session_ms == pytest.approx(sim.now * 1000, abs=2)


def test_second_session_have_and_settings_persist(libpath):
    sim = make(libpath, slot=SLOT, always_begin=True,
               config={**CbuScript().config, P.CFG_SYNC_MIN: 45, P.CFG_T_OFF: -50})
    assert sim.run_session() == P.RES_OK
    mcu = sim.mcu
    assert mcu.frame_addr() == SLOT_A                  # empty flash: A first
    seq1 = mcu.lib.h_settings_seq()
    progs = mcu.stats.programs
    sim.now += 10
    assert sim.run_session() == P.RES_OK
    assert sim.cbus[-1].img_result == "have"
    assert sim.cbus[-1].hello_rsp.last_result == P.RES_OK
    assert mcu.stats.programs == progs                   # nothing written
    assert mcu.lib.h_settings_seq() == seq1              # identical CONFIG: no write
    crc = sim.cbus[-1].hello_rsp.settings_crc
    assert crc == P.settings_crc({P.CFG_SYNC_MIN: 45, P.CFG_T_OFF: -50})
    mcu.lib.h_reload()                                   # "reset": read back from flash
    assert mcu.lib.h_settings_crc() == crc and mcu.setting(P.CFG_T_OFF) == -50
    assert mcu.frame_id == BEGIN.frame_id


def test_settings_ping_pong(libpath):
    mcu = CMcu(libpath)
    for i, sync in enumerate((30, 45, 90)):
        sim = make(libpath, mcu=mcu, config={**CbuScript().config, P.CFG_SYNC_MIN: sync})
        sim.now = i * 100.0
        assert sim.run_session() == P.RES_OK
        assert mcu.lib.h_settings_sector() == (0x1FC00, 0x1FE00, 0x1FC00)[i]
    mcu.lib.h_reload()
    assert mcu.setting(P.CFG_SYNC_MIN) == 90 and mcu.lib.h_settings_seq() == 3


def test_lost_hello_rsp_falls_back_to_115200(libpath):
    sim = make(libpath, fault=drop_nth("m2c", P.HELLO_RSP), slot=SLOT)
    assert sim.run_session() == P.RES_OK
    rsps = [f for f in frames(sim, "mcu-tx") if f.cmd == P.HELLO_RSP]
    assert [P.HelloRsp.parse(f.payload).baud for f in rsps] == [P.BAUD_FAST, P.BAUD_SLOW]
    assert sim.mcu.outcome.committed


def test_lost_probe_ack_converges(libpath):
    sim = make(libpath, fault=drop_nth("m2c", P.ACK, acked=P.PING), slot=SLOT)
    assert sim.run_session() == P.RES_OK
    assert sim.cbus[0].ep.sticky_slow and sim.mcu.stats.dup_requests >= 1


def test_duplicate_is_reacked_not_reapplied(libpath):
    sim = make(libpath, fault=drop_nth("m2c", P.ACK, n=1, acked=P.IMG_DATA), slot=SLOT)
    assert sim.run_session() == P.RES_OK
    assert sim.mcu.stats.dup_requests == 1 and sim.mcu.stats.reverts == 1
    assert sim.cbus[0].ep.stats.retries == 2
    assert sim.mcu.mem(SLOT_A + 32, len(PAYLOAD)) == PAYLOAD


def test_bad_fast_link_reverts_both(libpath):
    ref = [None]

    def fault(d, data, now):
        s = ref[0]
        fast = s.mcu.baud == P.BAUD_FAST or (s.cbu and s.cbu.ep.baud == P.BAUD_FAST)
        return bytes(len(data)) if fast else data
    sim = make(libpath, fault=fault, slot=SLOT)
    ref[0] = sim
    assert sim.run_session() == P.RES_OK
    assert sim.mcu.frame_id == BEGIN.frame_id


def test_corrupted_chunk_mid_stream(libpath):
    hits = [0]

    def fault(d, data, now):
        if d == "c2m" and data[3:4] == bytes([P.IMG_DATA]) and hits[0] == 0:
            hits[0] = 1
            b = bytearray(data)
            b[100] ^= 0x40
            return bytes(b)
        return data
    sim = make(libpath, fault=fault, slot=SLOT)
    assert sim.run_session() == P.RES_OK
    assert sim.mcu.mem(SLOT_A + 32, len(PAYLOAD)) == PAYLOAD


def test_bad_crc_frame_is_not_committed(libpath):
    def fault(d, data, now):
        if d == "c2m" and data[3:4] == bytes([P.IMG_DATA]):
            f = P.decode_frame(data)
            if P.u32(f.payload, 0) == 0:
                p = bytearray(f.payload)
                p[10] ^= 1
                return P.encode_frame(f.seq, f.cmd, bytes(p))
        return data
    sim = make(libpath, fault=fault, slot=SLOT)
    sim.run_session()
    assert sim.mcu.frame_id == 0 and sim.cbus[0].img_result == "crc refused"
    assert sim.mcu.mem(SLOT_A + 28, 4) == b"\xff" * 4          # commit word still erased


class Mute(FakeCbu):
    def _script(self):
        yield ("sleep", 100.0)


class Tuya(FakeCbu):
    def _script(self):
        self.ep.tx += b"\x00" + bytes.fromhex("55aa0001000000")
        yield ("sleep", 100.0)


def test_no_hello_retries_then_24h(libpath):
    sim = make(libpath, cbu=Mute)
    got = []
    for _ in range(3):
        got.append((sim.run_session(), sim.mcu.outcome.next_s))
        sim.now += 1
    assert got == [(P.RES_NO_HELLO, 60), (P.RES_NO_HELLO, 300), (P.RES_NO_HELLO, 24 * 3600)]
    o = sim.mcu.outcome
    assert o.status == 2 and o.icon == LINK_ERR and o.session_ms == pytest.approx(5000, abs=1)


def test_tuya_ends_at_once(libpath):
    sim = make(libpath, cbu=Tuya)
    assert sim.run_session() == P.RES_TUYA and sim.now < 0.5


def test_net_fail_grace(libpath):
    sim = make(libpath, net_fail=P.NET_FAIL_WIFI)
    assert sim.run_session() == P.RES_NET_FAIL
    assert sim.mcu.outcome.next_s == 2 * 3600 and sim.mcu.outcome.icon == LINK_ERR


def test_cap(libpath):
    sim = make(libpath, wifi_s=60.0)
    assert sim.run_session() == P.RES_CAP
    assert sim.mcu.outcome.session_ms == pytest.approx(45000, abs=2)


def test_hold_extends(libpath):
    sim = make(libpath, hold_s=120, hold_dwell=100.0)
    assert sim.run_session() == P.RES_OK
    assert 100_000 < sim.mcu.outcome.session_ms < 125_000


def test_low_battery_skips(libpath):
    mcu = CMcu(libpath)
    mcu.batt_mv = 3500
    assert not mcu.start_session(0.0) and not mcu.cbu_on


def test_abort(libpath):
    mcu = CMcu(libpath)
    mcu.start_session(0.0)
    mcu.lib.h_abort(1000)
    assert not mcu.cbu_on and mcu.result == P.RES_ABORT and mcu.outcome.icon == 0


def test_backoff_and_calendar(libpath):
    lib = CMcu(libpath).lib
    assert [lib.h_next_after(P.RES_NET_FAIL, 60, f, 0) for f in (1, 2, 3, 4)] == \
        [7200, 14400, 21600, 21600]
    assert lib.h_next_after(P.RES_OK, 60, 0, 0) == 3600
    assert lib.h_days(2026, 11, 1) == 1793520000 // 86400
    assert lib.h_days(2000, 3, 1) == 11017


# --- single requests ------------------------------------------------------------

class Direct:
    """Talk to the C engine frame by frame, LINKED at 115200."""

    def __init__(self, libpath, **kw):
        self.m = CMcu(libpath, **kw)
        self.t = 0.0
        self.seq = itertools.count(1)
        assert self.m.start_session(0.0)
        r = self.req(P.HELLO, P.Hello().encode(), seq=0)
        assert r.cmd == P.HELLO_RSP
        self.m.tx_done(0.001)                 # switch to 500 k, probe opens
        self.t = 0.2
        self.m.poll(self.t)                   # probe window over: back to 115200, LINKED
        assert self.m.baud == P.BAUD_SLOW

    def req(self, cmd, payload=b"", seq=None):
        seq = next(self.seq) if seq is None else seq
        self.t += 0.001
        self.m.feed(P.encode_frame(seq, cmd, payload), self.t)
        out = self.m.take_tx()
        return P.decode_frame(out) if out else None

    def err(self, cmd, payload=b""):
        r = self.req(cmd, payload)
        assert r.cmd == P.ERR, P.describe(r)
        e = P.Err.parse(r.payload)
        return e.code, e.detail


@pytest.mark.parametrize("bye,clears", [
    (bytes([P.BYE_DONE]), False),
    (bytes([P.BYE_DONE, P.BYE_F_HISTORY_ACK]), True),
    (bytes([P.BYE_NET_FAIL, P.BYE_F_HISTORY_ACK]), False),
])
def test_history_ack_only_clears_hello_snapshot(libpath, bye, clears):
    d = Direct(libpath)
    base = 1800000000 // 3600 * 3600
    d.m.lib.history_sample(base, True, 2100, 5000)
    d.m.lib.history_sample(base + 3600, True, 2200, 5100)
    rsp = d.req(P.HELLO, P.Hello(flags=P.HELLO_F_SLOW_ONLY).encode())
    hrs = P.HelloRsp.parse(rsp.payload).history
    assert len(hrs) == 1 and hrs[0].hour_utc == base
    d.m.lib.history_sample(base + 7200, True, 2300, 5200)
    assert d.req(P.BYE, bye).cmd == P.ACK
    out = (ctypes.c_uint8 * 138)()
    token = ctypes.c_uint32()
    d.m.lib.history_snapshot(out, ctypes.byref(token), 8)
    assert out[0] == (1 if clears else 2)
    assert int.from_bytes(bytes(out[2:6]), "little") == base + (3600 if clears else 0)


@pytest.mark.parametrize("cmd,payload,code,detail", [
    (P.IMG_BEGIN, P.ImgBegin(0, 2, 0, 0, 24960, 0).encode(), P.E_BAD_VALUE, 0),
    (P.IMG_BEGIN, P.ImgBegin(1, 3, 0, 0, 24960, 0).encode(), P.E_BAD_VALUE, 8),
    (P.IMG_BEGIN, P.ImgBegin(1, 2, 1, 0, 24960, 0).encode(), P.E_BAD_VALUE, 9),
    (P.IMG_BEGIN, P.ImgBegin(1, 2, 0, 0, 30000, 0).encode(), P.E_TOO_BIG, 25056),
    (P.IMG_BEGIN, P.ImgBegin(1, 2, 0, 0, 12480, 0).encode(), P.E_TOO_BIG, 25056),
    (P.IMG_DATA, P.img_data_encode(0, b"1234"), P.E_STATE, 0),
    (P.IMG_END, P.img_end_encode(1), P.E_STATE, 0),
    (P.TIME, P.Time(5).encode(), P.E_BAD_VALUE, 0),
    (P.TIME, P.Time(1800000000, 2000).encode(), P.E_BAD_VALUE, 4),
    (P.TIME, P.Time(1800000000, 0, 1700000000).encode(), P.E_BAD_VALUE, 6),
    (P.TIME, P.Time(1800000000, 0, 0, -1100).encode(), P.E_BAD_VALUE, 10),
    (P.CONFIG, P.config_encode({P.CFG_CLOCK_MIN: 0}), P.E_BAD_VALUE, P.CFG_CLOCK_MIN),
    (P.CONFIG, bytes([2, 1, 30]), P.E_BAD_LEN, 0),
    (P.CONFIG, bytes([1, 1, 1, 9, 5, 0]), P.E_BAD_LEN, 3),
    (P.NET, b"\x01", P.E_BAD_LEN, 3),
    (P.FORGET, b"", P.E_UNKNOWN_CMD, 0),
    (0x33, b"", P.E_UNKNOWN_CMD, 0),
    (P.PING, bytes(17), P.E_BAD_LEN, 16),
    (P.HOLD, P.hold_encode(901), P.E_BAD_VALUE, 0),
    (P.TIME, P.Time(4102444800).encode(), P.E_BAD_VALUE, 0),             # 2100: RTC year
    (P.TIME, P.Time(4102444800 - 1800, 60).encode(), P.E_BAD_VALUE, 0),  # local time in 2100
    (P.TIME, P.Time(1800000000, 0, 4102444800, 0).encode(), P.E_BAD_VALUE, 6),
])
def test_errors(libpath, cmd, payload, code, detail):
    assert Direct(libpath).err(cmd, payload) == (code, detail)


def test_config_unknown_tag_skipped_and_atomic(libpath):
    d = Direct(libpath)
    # unknown tag 0x7F skipped; SYNC_MIN applied
    r = d.req(P.CONFIG, bytes([0x7F, 3, 1, 2, 3]) + P.config_encode({P.CFG_SYNC_MIN: 20}))
    assert r.cmd == P.ACK and d.m.setting(P.CFG_SYNC_MIN) == 20
    # a bad value later in the list: nothing applied
    assert d.err(P.CONFIG, P.config_encode({P.CFG_SYNC_MIN: 30, P.CFG_FLAGS: 16})) == \
        (P.E_BAD_VALUE, P.CFG_FLAGS)
    assert d.m.setting(P.CFG_SYNC_MIN) == 20


def test_band_flags_preserve_existing_bits(libpath):
    d = Direct(libpath)
    for flags in (4, 8, 15, 0):
        assert d.req(P.CONFIG, P.config_encode({P.CFG_FLAGS: flags})).cmd == P.ACK
        assert d.m.setting(P.CFG_FLAGS) == flags


def test_duplicate_config(libpath):
    d = Direct(libpath)
    raw = P.encode_frame(9, P.CONFIG, P.config_encode({P.CFG_SYNC_MIN: 30}))
    d.m.feed(raw, 1.0)
    first = d.m.take_tx()
    d.m.feed(raw, 1.1)
    assert d.m.take_tx() == first and d.m.stats.settings_writes == 1
    assert d.m.stats.dup_requests == 1
    d.m.feed(P.encode_frame(9, P.CONFIG, P.config_encode({P.CFG_SYNC_MIN: 45})), 1.2)
    assert d.m.stats.settings_writes == 2


def test_img_data_offsets(libpath):
    d = Direct(libpath)
    assert d.req(P.IMG_BEGIN, BEGIN.encode()).payload == bytes([P.IMG_BEGIN, P.IMG_START])
    a = d.req(P.IMG_DATA, P.img_data_encode(0, PAYLOAD[:1020]))
    assert P.u32(a.payload, 1) == 1020
    assert d.err(P.IMG_DATA, P.img_data_encode(2040, PAYLOAD[2040:3060])) == (P.E_OFFSET, 1020)
    a = d.req(P.IMG_DATA, P.img_data_encode(0, PAYLOAD[:1020]))          # late repeat
    assert P.u32(a.payload, 1) == 1020
    assert d.err(P.IMG_DATA, P.img_data_encode(1020, b"123")) == (P.E_BAD_LEN, 4)
    assert d.err(P.IMG_DATA, P.img_data_encode(24960, b"1234")) == (P.E_TOO_BIG, 24960)
    assert d.err(P.IMG_END, P.img_end_encode(BEGIN.frame_id + 1)) == (P.E_BAD_VALUE, 0)
    assert d.err(P.IMG_END, P.img_end_encode(BEGIN.frame_id)) == (P.E_OFFSET, 1020)
    # HELLO aborts the transfer
    assert d.req(P.HELLO, P.Hello().encode()).cmd == P.HELLO_RSP
    assert d.err(P.IMG_DATA, P.img_data_encode(1020, PAYLOAD[1020:2040])) == (P.E_STATE, 0)


def test_requests_before_hello_and_after_bye(libpath):
    m = CMcu(libpath)
    m.start_session(0.0)
    m.feed(P.encode_frame(1, P.TIME, P.Time(1800000000).encode()), 0.1)
    e = P.Err.parse(P.decode_frame(m.take_tx()).payload)
    assert e.code == P.E_STATE
    d = Direct(libpath)
    assert d.req(P.BYE, P.bye_encode(P.BYE_DONE)).cmd == P.ACK
    assert d.err(P.PING, b"") == (P.E_STATE, 0)


def test_hold_granted(libpath):
    d = Direct(libpath)
    r = d.req(P.HOLD, P.hold_encode(100))
    assert r.cmd == P.ACK and P.u16(r.payload, 1) == 100
    r = d.req(P.HOLD, P.hold_encode(900))
    assert P.u16(r.payload, 1) == 899                  # capped at t0 + 900 s


def test_dst_switch(libpath):
    d = Direct(libpath)
    utc = 1806195600 - 90                               # 90 s before the change
    assert d.req(P.TIME, P.Time(utc, 60, 1806195600, 120).encode()).cmd == P.ACK
    assert d.m.rtc()[4:6] == (1, 58)                    # 2027-03-28 01:58:30 local (+1)
    lib = d.m.lib
    assert not lib.h_minute()
    # 2 minutes later: 02:00:30 local (+1) = 01:00:30 UTC, past the change
    lib.h_rtc_set((ctypes.c_uint8 * 7)(27, 3, 28, 0, 2, 0, 30))
    assert lib.h_minute()
    assert d.m.rtc()[4:7] == (3, 0, 30)                 # now +2: 03:00:30
    assert not lib.h_minute()                           # applied once


# --- review fixes ------------------------------------------------------------------

def test_closing_bounded_without_tc(libpath):
    """BYE ACKed but TC never comes: CLOSING still ends 100 ms after BYE."""
    d = Direct(libpath)
    assert d.req(P.BYE, P.bye_encode(P.BYE_DONE)).cmd == P.ACK   # no tx_done follows
    t_bye = d.t
    d.m.poll(t_bye + 0.090)
    assert d.m.cbu_on
    assert d.m.next_deadline() <= t_bye + 0.101
    d.m.poll(t_bye + 0.101)
    assert not d.m.cbu_on and d.m.outcome.result == P.RES_OK


def test_closing_normal_path_unchanged(libpath):
    d = Direct(libpath)
    assert d.req(P.BYE, P.bye_encode(P.BYE_DONE)).cmd == P.ACK
    d.m.tx_done(d.t + 0.001)
    d.m.poll(d.t + 0.050)
    assert d.m.cbu_on
    d.m.poll(d.t + 0.052)
    assert not d.m.cbu_on and d.m.outcome.result == P.RES_OK


def test_hello_after_bye_refused(libpath):
    """A CBU that reboots between BYE and power-off cannot turn OK into CAP."""
    d = Direct(libpath)
    assert d.req(P.BYE, P.bye_encode(P.BYE_DONE)).cmd == P.ACK
    assert d.err(P.HELLO, P.Hello().encode()) == (P.E_STATE, 0)
    d.m.tx_done(d.t + 0.001)
    d.m.poll(d.t + 0.060)
    assert not d.m.cbu_on and d.m.outcome.result == P.RES_OK


def test_revert_waits_for_tx_idle(libpath):
    """R1b decided while an ACK is still in the shifter: the port keeps 500 k
    until TC, then drops to 115200."""
    m = CMcu(libpath)
    assert m.start_session(0.0)
    m.feed(P.encode_frame(0, P.HELLO, P.Hello().encode()), 0.001)
    assert P.decode_frame(m.take_tx()).cmd == P.HELLO_RSP
    m.tx_done(0.002)
    assert m.baud == P.BAUD_FAST
    m.feed(P.encode_frame(1, P.PING, b"x"), 0.005)                 # probe: LINKED at 500 k
    assert P.decode_frame(m.take_tx()).cmd == P.ACK
    m.tx_done(0.006)
    m.feed(P.encode_frame(2, P.PING, b"y"), 0.010)                 # ACK now in flight
    assert P.decode_frame(m.take_tx()).cmd == P.ACK
    m.feed(b"\x00\x13\x77", 0.070)                                 # garbage 60 ms later: R1b
    assert m.stats.reverts == 1
    assert m.baud == P.BAUD_FAST                                    # still sending the ACK
    m.tx_done(0.071)
    assert m.baud == P.BAUD_SLOW


def test_hello_slow_only_flag(libpath):
    """HELLO flag 0x01: the MCU offers 115200 and never probes."""
    sim = make(libpath, slot=SLOT, slow_only=True)
    assert sim.run_session(P.TRIG_BUTTON) == P.RES_OK
    cbu = sim.cbus[0]
    assert cbu.hello_rsp.baud == P.BAUD_SLOW and cbu.img_result == "committed"
    assert sim.mcu.stats.reverts == 0 and sim.mcu.baud == P.BAUD_SLOW
    assert not any(f.cmd == P.PING for f in frames(sim, "mcu-rx"))


# --- review round 2 -----------------------------------------------------------------

def test_cap_without_hold_is_a_failure(libpath):
    sim = make(libpath, wifi_s=60.0)
    assert sim.run_session() == P.RES_CAP
    o = sim.mcu.outcome
    assert o.fail_count == 1 and o.icon == LINK_ERR and o.next_s == 2 * 3600


def test_maintenance_hold_ending_in_cap_is_not_a_failure(libpath):
    """A granted HOLD (maintenance/OTA) that runs into the 900 s cap: result CAP for
    HA, but scheduled like OK, fail_count kept, icon as for ABORT (unchanged)."""
    sim = make(libpath, hold_s=900, hold_dwell=2000.0)
    assert sim.run_session() == P.RES_CAP
    o = sim.mcu.outcome
    assert o.session_ms == pytest.approx(900_000, abs=5)
    assert o.fail_count == 0 and o.icon == 0 and o.next_s == 60 * 60   # LINK_WAIT kept


def test_hold_refused_in_grace(libpath):
    d = Direct(libpath)
    assert d.req(P.NET, P.Net(P.NET_FAIL_WIFI).encode()).cmd == P.ACK
    assert d.err(P.HOLD, P.hold_encode(900)) == (P.E_STATE, 0)
    d.m.poll(d.t + 2.1)                                   # grace still ends it
    assert not d.m.cbu_on and d.m.outcome.result == P.RES_NET_FAIL


def test_factory_reset_sends_forget(libpath):
    mcu = CMcu(libpath)
    mcu.lib.h_factory_reset()
    sim = make(libpath, mcu=mcu)
    assert sim.run_session(P.TRIG_BOOT) == P.RES_OK
    hr = sim.cbus[0].hello_rsp
    assert hr.flags & P.F_FORGET_PENDING and hr.flags & P.F_FACTORY_RESET
    assert any(f.cmd == P.FORGET for f in frames(sim, "mcu-tx"))
    # FORGET acknowledged: not pending any more, FACTORY_RESET cleared by the OK
    sim2 = make(libpath, mcu=mcu)
    sim2.now = 100.0
    assert sim2.run_session() == P.RES_OK
    assert not sim2.cbus[0].hello_rsp.flags & (P.F_FORGET_PENDING | P.F_FACTORY_RESET)


def test_units_are_local_and_not_in_the_crc(libpath):
    mcu = CMcu(libpath)
    crc0 = mcu.lib.h_settings_crc()
    assert mcu.lib.h_set_units(1) == 0 and mcu.lib.h_units() == 1
    assert mcu.lib.h_settings_crc() == crc0 == 0xFAFE77C2
    writes = mcu.lib.h_settings_seq()
    assert mcu.lib.h_set_units(1) == 0 and mcu.lib.h_settings_seq() == writes   # unchanged: no write
    rec = mcu.mem(mcu.lib.h_settings_sector(), 46)
    assert struct.unpack_from("<IHH", rec) == (0x53354854, 1, 30)
    assert rec[12 + 27:12 + 30] == bytes([0x81, 1, 1])
    # a CONFIG keeps the units; a reload reads them back
    sim = make(libpath, mcu=mcu, config={**CbuScript().config, P.CFG_SYNC_MIN: 30})
    assert sim.run_session() == P.RES_OK
    mcu.lib.h_reload()
    assert mcu.lib.h_units() == 1 and mcu.setting(P.CFG_SYNC_MIN) == 30


def test_old_settings_record_without_units_loads(libpath):
    mcu = CMcu(libpath)
    vals = {P.CFG_MODE: 0, P.CFG_SYNC_MIN: 45, P.CFG_CLOCK_MIN: 5, P.CFG_CLOCK_24H: 0,
            P.CFG_FULL_N: 10, P.CFG_T_OFF: -20, P.CFG_RH_OFF: 30, P.CFG_FLAGS: 1}
    tlv = P.config_encode(vals)
    assert len(tlv) == 27
    body = struct.pack("<IHHI", 0x53354854, 1, 27, 7) + tlv
    rec = body + struct.pack("<I", __import__("zlib").crc32(body))
    mcu.lib.h_load(0x1FE00, rec, len(rec))
    mcu.lib.h_reload()
    assert mcu.lib.h_units() == 0 and mcu.lib.h_settings_seq() == 7
    assert all(mcu.setting(k) == v for k, v in vals.items())


def test_settings_erase(libpath):
    mcu = CMcu(libpath)
    assert mcu.lib.h_set_units(1) == 0
    assert mcu.lib.h_settings_erase() == 1 and mcu.lib.h_settings_factory() == 1
    assert mcu.mem(0x1FC00, 1024) == b"\xff" * 1024
    mcu.lib.h_reload()
    assert mcu.lib.h_units() == 0 and mcu.lib.h_settings_crc() == 0xFAFE77C2


# --- MCU firmware update (protocol.md 4.14-4.15, design 4.7) -------------------------

STAGE, BOOT_INFO = 0x10000, 0xC0
APP_VERSION = int(re.search(r"#define LINK_FW_VERSION\s+(0x[0-9A-Fa-f]+)u",
                            (ROOT / "firmware/src/link.h").read_text()).group(1), 16)
IMAGE = bytes((i * 37 + 11) & 0xFF for i in range(P.FWU_MAX_IMAGE))   # the largest image
FWU = P.fwu_package(IMAGE, 0x00020001)


def with_boot(mcu: CMcu, ver: int = 1) -> CMcu:
    """boot_ver() reads the bootloader's info block at 0xC0 of host flash."""
    mcu.lib.h_load(BOOT_INFO, struct.pack("<II", P.FWU_BOOT_MAGIC, ver), 8)
    return mcu


def stage_hdr(mcu):
    return struct.unpack("<8I", mcu.mem(STAGE, 32))


def test_firmware_session_stages_the_image(libpath):
    """A frame in slot A; the package replaces it. 50 chunks of image offsets,
    then FW_END commits the staging header for the bootloader."""
    mcu = with_boot(CMcu(libpath, slot_a=SLOT))
    assert mcu.frame_id == BEGIN.frame_id
    sim = make(libpath, mcu=mcu, fwu=FWU, slot=SLOT, always_begin=True)
    assert sim.run_session(P.TRIG_BUTTON) == P.RES_OK
    cbu = sim.cbus[0]
    assert cbu.error is None and cbu.hello_rsp.boot_ver == 1 and cbu.fw_result == "staged"
    o = mcu.outcome
    assert o.fw_staged and not o.committed and o.icon == LINK_OK
    rx = frames(sim, "mcu-rx")
    assert P.IMG_BEGIN not in [f.cmd for f in rx] and [f.cmd for f in rx][-1] == P.BYE
    data = [P.img_data_parse(f.payload) for f in rx if f.cmd == P.IMG_DATA]
    assert len(data) == 50 and [off for off, _ in data] == [i * 1020 for i in range(50)]
    assert mcu.frame_id == 0 and mcu.frame_addr() is None           # both slots invalid
    assert stage_hdr(mcu) == (P.FWU_MAGIC, 0x00020001, len(IMAGE), P.crc32(IMAGE), 0x1000, 1,
                              0, 0xFFFFFFFF)                         # committed, not yet done
    assert mcu.mem(STAGE + 32, len(IMAGE)) == IMAGE
    # at 500 k the whole session is short; the transfer itself is ~1.3 s
    assert o.session_ms < 6000


def test_firmware_session_at_115200_fits_the_cap(libpath):
    """The largest image at 115200 (fast_baud off): well inside 45 s."""
    mcu = with_boot(CMcu(libpath))
    sim = make(libpath, mcu=mcu, fwu=FWU, slow_only=True)
    assert sim.run_session() == P.RES_OK and mcu.outcome.fw_staged
    rx = [(t, f) for t, d, f in sim.trace if d == "mcu-rx"]
    t0 = next(t for t, f in rx if f.cmd == P.FW_BEGIN)
    t1 = next(t for t, f in rx if f.cmd == P.FW_END)
    assert 4.0 < t1 - t0 < 6.5                                      # ~50 x 1028 B at 11.5 kB/s


def test_firmware_have(libpath):
    """The running app already has this version and the installed region this CRC."""
    app = IMAGE[:32000]
    mcu = with_boot(CMcu(libpath, slot_a=SLOT))
    mcu.lib.h_load(0x1000, app, len(app))
    mcu.lib.h_reload()
    progs = mcu.stats.programs
    sim = make(libpath, mcu=mcu, fwu=P.fwu_package(app, APP_VERSION))
    assert sim.run_session() == P.RES_OK
    assert sim.cbus[0].fw_result == "have"
    assert not any(f.cmd in (P.IMG_DATA, P.FW_END) for f in frames(sim, "mcu-rx"))
    assert not mcu.outcome.fw_staged and mcu.frame_id == BEGIN.frame_id   # nothing touched
    assert mcu.stats.programs == progs and mcu.mem(STAGE + 0x6200, 4) != b"TH5U"


def test_firmware_refused_without_bootloader(libpath):
    mcu = CMcu(libpath, slot_a=SLOT)                                # 0xC0 erased: boot_ver 0
    sim = make(libpath, mcu=mcu, fwu=FWU)
    assert sim.run_session() == P.RES_OK
    cbu = sim.cbus[0]
    assert cbu.hello_rsp.boot_ver == 0
    assert cbu.fw_result == f"refused {P.Err(P.FW_BEGIN, P.E_STATE, P.FWS_NO_BOOT)}"
    assert mcu.frame_id == BEGIN.frame_id and not mcu.outcome.fw_staged


def test_firmware_bad_crc_at_fw_end(libpath):
    def fault(d, data, now):
        if d == "c2m" and data[3:4] == bytes([P.IMG_DATA]):
            f = P.decode_frame(data)
            if P.u32(f.payload, 0) == 10200:
                p = bytearray(f.payload)
                p[10] ^= 1
                return P.encode_frame(f.seq, f.cmd, bytes(p))
        return data
    mcu = with_boot(CMcu(libpath))
    sim = make(libpath, mcu=mcu, fault=fault, fwu=FWU)
    assert sim.run_session() == P.RES_OK
    bad = bytearray(IMAGE)
    bad[10200 + 6] ^= 1
    assert sim.cbus[0].fw_result == f"refused {P.Err(P.FW_END, P.E_CRC, P.crc32(bytes(bad)))}"
    assert not mcu.outcome.fw_staged
    assert stage_hdr(mcu)[6:] == (0xFFFFFFFF, 0xFFFFFFFF)            # commit word still erased


def test_firmware_requests(libpath):
    d = Direct(libpath)
    b = P.FwBegin(0x00020001, 2040, P.crc32(IMAGE[:2040]))
    assert d.err(P.FW_BEGIN, b.encode()) == (P.E_STATE, P.FWS_NO_BOOT)
    with_boot(d.m)
    assert d.err(P.FW_END, P.fw_end_encode(b.crc32)) == (P.E_STATE, 0)
    assert d.err(P.FW_BEGIN, P.FwBegin(1, 64, 0, 0x2000).encode()) == (P.E_BAD_VALUE, 12)
    for n in (60, 66, 50148):
        assert d.err(P.FW_BEGIN, P.FwBegin(1, n, 0).encode()) == (P.E_TOO_BIG, 50144)
    assert d.req(P.FW_BEGIN, b.encode()).payload == bytes([P.FW_BEGIN, P.IMG_START])
    assert P.u32(d.req(P.IMG_DATA, P.img_data_encode(0, IMAGE[:1020])).payload, 1) == 1020
    assert d.err(P.IMG_END, P.img_end_encode(1)) == (P.E_STATE, 0)
    assert d.err(P.FW_END, P.fw_end_encode(b.crc32 ^ 1)) == (P.E_BAD_VALUE, 0)
    assert d.err(P.FW_END, P.fw_end_encode(b.crc32)) == (P.E_OFFSET, 1020)
    assert P.u32(d.req(P.IMG_DATA, P.img_data_encode(1020, IMAGE[1020:2040])).payload, 1) == 2040
    assert d.req(P.FW_END, P.fw_end_encode(b.crc32)).payload == bytes([P.FW_END])
    assert d.m.outcome.fw_staged
    # after FW_END: no more frames or firmware in this session
    assert d.err(P.IMG_BEGIN, BEGIN.encode()) == (P.E_STATE, P.FWS_STAGED)
    assert d.err(P.FW_BEGIN, b.encode()) == (P.E_STATE, P.FWS_STAGED)


def test_fw_begin_abandons_frame_transfer(libpath):
    d = Direct(libpath)
    with_boot(d.m)
    assert d.req(P.IMG_BEGIN, BEGIN.encode()).payload == bytes([P.IMG_BEGIN, P.IMG_START])
    assert d.req(P.IMG_DATA, P.img_data_encode(0, PAYLOAD[:1020])).cmd == P.ACK
    b = P.FwBegin(0x00020001, 1020, P.crc32(IMAGE[:1020]))
    assert d.req(P.FW_BEGIN, b.encode()).payload == bytes([P.FW_BEGIN, P.IMG_START])
    assert d.err(P.IMG_END, P.img_end_encode(BEGIN.frame_id)) == (P.E_STATE, 0)
    assert d.req(P.IMG_DATA, P.img_data_encode(0, IMAGE[:1020])).cmd == P.ACK
    # and IMG_BEGIN abandons the firmware transfer
    assert d.req(P.IMG_BEGIN, BEGIN.encode()).payload == bytes([P.IMG_BEGIN, P.IMG_START])
    assert d.err(P.FW_END, P.fw_end_encode(b.crc32)) == (P.E_STATE, 0)
