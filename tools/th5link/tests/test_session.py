"""Loopback sessions: the fake CBU against the MCU model in virtual time.
This is the executable form of protocol.md sections 2.3, 3 and 5."""
from pathlib import Path

import pytest

from tools.th5link import proto as P
from tools.th5link.fakecbu import CbuScript, FakeCbu
from tools.th5link.mcumodel import (LINK_ERR, LINK_OK, LINK_WAIT, STATUS_LINK_ERROR, McuModel,
                                    next_after)
from tools.th5link.sim import Sim, frames

ROOT = Path(__file__).resolve().parents[3]
SLOT = (ROOT / "ha/out/frame-C.slot").read_bytes()
BEGIN, PAYLOAD = P.ImgBegin.from_slot(SLOT)


def make(fault=None, mcu=None, **kw):
    mcu = mcu or McuModel()
    return Sim(mcu, lambda: FakeCbu(CbuScript(**kw)), fault=fault)


def drop_nth(direction, cmd, n=1, acked=None):
    """Fault hook: drop the n-th chunk going `direction` that holds a frame
    with `cmd` (and, for ACKs, acknowledging `acked`)."""
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
    fault.seen = seen
    return fault


def test_full_session_with_frame():
    sim = make(slot=SLOT)
    assert sim.run_session(P.TRIG_BUTTON) == P.RES_OK
    m = sim.mcu
    assert m.frame_id == BEGIN.frame_id and bytes(m.active_slot().data) == PAYLOAD
    assert m.time.offset_min == 60 and m.time.next_offset_min == 120
    assert m.settings.values[P.CFG_SYNC_MIN] == 60
    assert m.link_icon == LINK_OK and m.refresh == "full"
    cbu = sim.cbus[0]
    assert cbu.img_result == "committed" and cbu.error is None
    assert cbu.hello_rsp.trigger == P.TRIG_BUTTON and cbu.hello_rsp.frame_id == 0
    # it ran at 500 k: 25 KB of frame in well under a second of UART time
    assert cbu.ep.stats.reverts == 0 and m.ep.stats.reverts == 0
    rx = frames(sim, "mcu-rx")
    assert [f.cmd for f in rx[:3]] == [P.HELLO, P.PING, P.NET]
    assert sum(f.cmd == P.IMG_DATA for f in rx) == 25
    assert m.next_session_in == 60 * 60
    # power off 50 ms after the BYE ACK left the wire
    t_bye = max(t for t, d, f in sim.trace if d == "mcu-tx" and f.cmd == P.ACK
                and f.payload[:1] == bytes([P.BYE]))
    assert sim.now - t_bye == pytest.approx(0.050, abs=0.002)
    assert m.last_session_ms == pytest.approx(sim.now * 1000, abs=1)


def test_second_session_skips_known_frame():
    sim = make(slot=SLOT)
    sim.run_session()
    writes = sim.mcu.settings_writes
    sim.run_session()
    assert sim.cbus[-1].img_result == "same id"
    assert sim.mcu.settings_writes == writes          # unchanged CONFIG: no flash write
    assert sim.cbus[-1].hello_rsp.last_result == P.RES_OK


def test_img_begin_have():
    m = McuModel()
    m.load_slot_image(SLOT)
    m.start_session(0.0)
    m.state = "LINKED"
    rsp = m._handle(P.Frame(1, P.IMG_BEGIN, BEGIN.encode()), 0.0)
    assert rsp == (P.ACK, bytes([P.IMG_BEGIN, P.IMG_HAVE]))


def test_duplicate_seq_is_reacked_not_reapplied():
    fault = drop_nth("m2c", P.ACK, n=1, acked=P.IMG_DATA)   # first IMG_DATA ACK lost
    sim = make(fault=fault, slot=SLOT)
    assert sim.run_session() == P.RES_OK
    assert sim.mcu.ep.stats.dup_requests == 1
    assert bytes(sim.mcu.active_slot().data) == PAYLOAD
    # R1a: the first retry goes at 115200 and is garbage to the MCU, which
    # reverts (R1b); the second retry is the one re-ACKed.
    assert sim.cbus[0].ep.stats.retries == 2
    assert sim.mcu.ep.stats.reverts == 1 and sim.cbus[0].ep.stats.reverts == 1


def test_duplicate_config_not_reapplied():
    m = McuModel()
    m.start_session(0.0)
    m.state = "LINKED"
    ep = m.ep
    raw = P.encode_frame(9, P.CONFIG, P.config_encode({P.CFG_SYNC_MIN: 30}))
    ep.feed(raw, 0.0)
    first = ep.take_tx()
    ep.feed(raw, 0.1)
    assert ep.take_tx() == first and m.settings_writes == 1 and ep.stats.dup_requests == 1
    # same seq but different content: a new request
    ep.feed(P.encode_frame(9, P.CONFIG, P.config_encode({P.CFG_SYNC_MIN: 45})), 0.2)
    assert m.settings_writes == 2


def test_lost_hello_rsp_falls_back_to_115200():
    sim = make(fault=drop_nth("m2c", P.HELLO_RSP), slot=SLOT)
    assert sim.run_session() == P.RES_OK
    rsps = [f for f in frames(sim, "mcu-tx") if f.cmd == P.HELLO_RSP]
    assert [P.HelloRsp.parse(f.payload).baud for f in rsps] == [P.BAUD_FAST, P.BAUD_SLOW]
    assert sim.mcu.committed_this_session


def test_lost_probe_ack_converges():
    sim = make(fault=drop_nth("m2c", P.ACK, acked=P.PING), slot=SLOT)
    assert sim.run_session() == P.RES_OK
    cbu = sim.cbus[0]
    assert cbu.ep.sticky_slow and cbu.ep.stats.reverts == 1
    assert sim.mcu.ep.stats.dup_requests >= 1          # the PING retry was re-ACKed


def test_bad_fast_link_reverts_both():
    """Every byte sent at 500 k is corrupted: the probe fails, the session
    still completes at 115200."""
    def fault(d, data, now):
        return bytes(len(data)) if sim_ref[0].mcu.ep.baud == P.BAUD_FAST or \
            (sim_ref[0].cbu and sim_ref[0].cbu.ep.baud == P.BAUD_FAST) else data
    sim_ref = [None]
    sim = make(fault=fault, slot=SLOT)
    sim_ref[0] = sim
    assert sim.run_session() == P.RES_OK
    assert sim.mcu.frame_id == BEGIN.frame_id


def test_corrupted_chunk_mid_stream():
    hits = [0]

    def fault(d, data, now):
        if d == "c2m" and data[3:4] == bytes([P.IMG_DATA]) and hits[0] == 0:
            hits[0] = 1
            b = bytearray(data)
            b[100] ^= 0x40                     # CRC now fails at the MCU
            return bytes(b)
        return data
    sim = make(fault=fault, slot=SLOT)
    assert sim.run_session() == P.RES_OK
    assert bytes(sim.mcu.active_slot().data) == PAYLOAD


def test_bad_crc_frame_is_not_committed():
    def fault(d, data, now):
        if d == "c2m" and data[3:4] == bytes([P.IMG_DATA]):
            f = P.decode_frame(data)
            if P.u32(f.payload, 0) == 0:
                p = bytearray(f.payload)
                p[10] ^= 1                     # valid frame, wrong pixel
                return P.encode_frame(f.seq, f.cmd, bytes(p))
        return data
    sim = make(fault=fault, slot=SLOT)
    sim.run_session()
    assert sim.mcu.frame_id == 0 and sim.cbus[0].img_result == "crc refused"
    errs = [P.Err.parse(f.payload) for f in frames(sim, "mcu-tx") if f.cmd == P.ERR]
    assert errs and errs[0].code == P.E_CRC


class Mute(FakeCbu):
    def _script(self):
        yield ("sleep", 100.0)


class Tuya(FakeCbu):
    def _script(self):
        self.ep.tx += b"\x00" + bytes.fromhex("55aa0001000000")
        yield ("sleep", 100.0)


def test_no_hello_retries_then_24h():
    sim = Sim(McuModel(), lambda: Mute(CbuScript()))
    results = []
    for _ in range(3):
        results.append((sim.run_session(), sim.mcu.next_session_in))
        sim.now += 1
    assert results == [(P.RES_NO_HELLO, 60), (P.RES_NO_HELLO, 300), (P.RES_NO_HELLO, 24 * 3600)]
    assert sim.mcu.status == STATUS_LINK_ERROR and sim.mcu.link_icon == LINK_ERR
    assert sim.mcu.last_session_ms == pytest.approx(5000, abs=1)


def test_tuya_ends_at_once():
    sim = Sim(McuModel(), lambda: Tuya(CbuScript()))
    assert sim.run_session() == P.RES_TUYA
    assert sim.now < 0.5


def test_net_fail_backoff():
    sim = make(net_fail=P.NET_FAIL_WIFI)
    assert sim.run_session() == P.RES_NET_FAIL
    assert sim.mcu.next_session_in == 2 * 3600 and sim.mcu.link_icon == LINK_ERR
    assert sim.run_session() == P.RES_NET_FAIL
    assert sim.mcu.next_session_in == 4 * 3600
    sim2 = make()
    sim2.mcu = sim.mcu
    sim2.mcu.ep.trace = None
    assert sim2.run_session() == P.RES_OK
    assert sim2.mcu.fail_count == 0 and sim2.mcu.next_session_in == 3600


def test_net_fail_without_bye_grace():
    class NoBye(FakeCbu):
        def _script(self):
            yield ("sleep", 0.3)
            yield self.req(P.HELLO, P.Hello().encode())
            yield self.req(P.NET, P.Net(P.NET_FAIL_API).encode())
            yield ("sleep", 100.0)
    sim = Sim(McuModel(), lambda: NoBye(CbuScript()))
    assert sim.run_session() == P.RES_NET_FAIL
    t_net = max(t for t, d, f in sim.trace if d == "mcu-rx" and f.cmd == P.NET)
    assert sim.now == pytest.approx(t_net + P.T_NET_GRACE, abs=0.01)


def test_no_ha_is_a_failure():
    sim = make(ha_present=False)
    assert sim.run_session() == P.RES_NET_FAIL


def test_cap():
    sim = make(wifi_s=100.0)
    assert sim.run_session() == P.RES_CAP
    assert sim.now == pytest.approx(P.T_CAP)


def test_hold_and_cbu_reboot():
    sim = make(hold_s=300, hold_dwell=120.0)
    sim.mcu.ep.trace = lambda d, f, t: sim.trace.append((t, "mcu-" + d, f))
    sim.mcu.start_session(0.0)
    sim._power()
    while sim.now < 60:
        sim.step()
    assert sim.mcu.cbu_on                               # past the 45 s cap
    sim.reboot_cbu(delay=0.5)                            # CBU crashes, boots again
    while sim.mcu.cbu_on and sim.now < 1000:
        sim.step()
    hellos = [f for f in frames(sim, "mcu-rx") if f.cmd == P.HELLO]
    assert len(hellos) == 2 and sim.mcu.result == P.RES_OK
    assert sim.now < 900


def test_hold_bounded_by_900s():
    m = McuModel()
    m.start_session(0.0)
    m.state = "LINKED"
    m._handle(P.Frame(1, P.HOLD, P.hold_encode(900)), 500.0)
    assert m.deadline == 900.0
    assert m._handle(P.Frame(2, P.HOLD, P.hold_encode(901)), 0)[0] == P.ERR


def test_forget():
    mcu = McuModel()
    mcu.forget_pending = True
    sim = make(mcu=mcu)
    assert sim.run_session() == P.RES_OK
    assert sim.cbus[0].forgotten and not mcu.forget_pending
    assert any(f.cmd == P.FORGET for f in frames(sim, "cbu-rx"))


def test_collision_cbu_wins():
    """MCU and CBU send requests at the same moment: the CBU drops the MCU's,
    the MCU answers the CBU's, and the MCU's retry gets through."""
    from tools.th5link.link import Endpoint
    cbu = Endpoint("cbu", lambda f, t: (P.ACK, P.ack_encode(f.cmd)), P.MAX_TO_CBU, P.MAX_TO_MCU)
    mcu = Endpoint("mcu", lambda f, t: (P.ACK, P.ack_encode(f.cmd)), P.MAX_TO_MCU, P.MAX_TO_CBU)
    got = {}
    cbu.request(P.NET, P.Net(1).encode(), 0.0, on_done=lambda f, t: got.setdefault("cbu", f))
    mcu.request(P.FORGET, b"", 0.0, on_done=lambda f, t: got.setdefault("mcu", f))
    c2m, m2c = cbu.take_tx(), mcu.take_tx()
    cbu.tx_done(0.001), mcu.tx_done(0.001)
    cbu.feed(m2c, 0.001)                     # dropped: CBU has one outstanding
    mcu.feed(c2m, 0.001)                     # answered
    assert cbu.stats.dropped_collisions == 1
    cbu.feed(mcu.take_tx(), 0.002)
    assert got["cbu"].cmd == P.ACK
    mcu.tick(0.3)                            # MCU retries FORGET
    cbu.feed(mcu.take_tx(), 0.3)
    mcu.feed(cbu.take_tx(), 0.3)
    assert got["mcu"].cmd == P.ACK


def test_link_failure_after_retries():
    from tools.th5link.link import Endpoint
    cbu = Endpoint("cbu", None, P.MAX_TO_CBU, P.MAX_TO_MCU)
    out = []
    cbu.request(P.TIME, P.Time(1793520000).encode(), 0.0, on_done=lambda f, t: out.append((f, t)))
    t = 0.0
    sends = 0
    while not out:
        if cbu.take_tx():
            sends += 1
            cbu.tx_done(t)
        t += 0.01
        cbu.tick(t)
    assert sends == 1 + P.RETRIES and out[0][0] is None
    assert out[0][1] == pytest.approx(4 * P.T_ACK, abs=0.02)


def test_requests_before_hello_refused():
    m = McuModel()
    m.start_session(0.0)
    assert m._handle(P.Frame(0, P.TIME, P.Time(1793520000).encode()), 0.0) == \
        (P.ERR, P.Err(P.TIME, P.E_STATE).encode())


def test_low_battery_skips_session():
    m = McuModel()
    m.reading.batt_mv = 3600
    assert not m.start_session(0.0) and not m.cbu_on and m.link_icon == LINK_WAIT


def test_switch_off_aborts():
    sim = make(wifi_s=10)
    sim.mcu.start_session(0.0)
    sim._power()
    while sim.now < 1.0:
        sim.step()
    sim.mcu.switch_off(sim.now)
    assert not sim.mcu.cbu_on and sim.mcu.last_result == P.RES_ABORT


@pytest.mark.parametrize("result,fails,nohello,expect", [
    (P.RES_OK, 0, 0, 3600),
    (P.RES_NET_FAIL, 1, 0, 7200),
    (P.RES_CAP, 3, 0, 6 * 3600),
    (P.RES_LINK, 9, 0, 6 * 3600),
    (P.RES_NO_HELLO, 1, 1, 60),
    (P.RES_TUYA, 2, 2, 300),
    (P.RES_NO_HELLO, 3, 3, 24 * 3600),
])
def test_backoff_table(result, fails, nohello, expect):
    assert next_after(result, 60, fails, nohello) == expect


@pytest.mark.parametrize("cmd,payload,code", [
    (P.IMG_BEGIN, P.ImgBegin(0, 2, 0, 0, 24960, 0).encode(), P.E_BAD_VALUE),
    (P.IMG_BEGIN, P.ImgBegin(1, 3, 0, 0, 24960, 0).encode(), P.E_BAD_VALUE),
    (P.IMG_BEGIN, P.ImgBegin(1, 2, 1, 0, 24960, 0).encode(), P.E_BAD_VALUE),
    (P.IMG_BEGIN, P.ImgBegin(1, 2, 0, 0, 30000, 0).encode(), P.E_TOO_BIG),
    (P.IMG_DATA, P.img_data_encode(0, b"1234"), P.E_STATE),
    (P.IMG_END, P.img_end_encode(1), P.E_STATE),
    (P.TIME, P.Time(5).encode(), P.E_BAD_VALUE),
    (P.CONFIG, P.config_encode({P.CFG_CLOCK_MIN: 0}), P.E_BAD_VALUE),
    (P.NET, b"\x01", P.E_BAD_LEN),
    (P.FORGET, b"", P.E_UNKNOWN_CMD),
    (0x33, b"", P.E_UNKNOWN_CMD),
    (P.PING, bytes(17), P.E_BAD_LEN),
])
def test_errors(cmd, payload, code):
    m = McuModel()
    m.start_session(0.0)
    m.state = "LINKED"
    rsp = m._handle(P.Frame(1, cmd, payload), 0.0)
    assert rsp[0] == P.ERR and P.Err.parse(rsp[1]).code == code


def test_img_data_offsets():
    m = McuModel()
    m.start_session(0.0)
    m.state = "LINKED"
    m._handle(P.Frame(1, P.IMG_BEGIN, BEGIN.encode()), 0)
    ack = m._handle(P.Frame(2, P.IMG_DATA, P.img_data_encode(0, PAYLOAD[:1020])), 0)
    assert P.u32(ack[1], 1) == 1020
    err = P.Err.parse(m._handle(P.Frame(3, P.IMG_DATA, P.img_data_encode(2040, PAYLOAD[2040:3060])), 0)[1])
    assert (err.code, err.detail) == (P.E_OFFSET, 1020)
    # a late repeat of an already written chunk (new seq) is ACKed, not rewritten
    ack = m._handle(P.Frame(4, P.IMG_DATA, P.img_data_encode(0, PAYLOAD[:1020])), 0)
    assert P.u32(ack[1], 1) == 1020
    err = P.Err.parse(m._handle(P.Frame(5, P.IMG_DATA, P.img_data_encode(1020, b"123")), 0)[1])
    assert err.code == P.E_BAD_LEN
    err = P.Err.parse(m._handle(P.Frame(6, P.IMG_END, P.img_end_encode(BEGIN.frame_id)), 0)[1])
    assert (err.code, err.detail) == (P.E_OFFSET, 1020)


def test_hello_mid_session_aborts_transfer():
    m = McuModel()
    m.start_session(0.0)
    m.state = "LINKED"
    m._handle(P.Frame(1, P.IMG_BEGIN, BEGIN.encode()), 0)
    assert m.img is not None
    m._handle(P.Frame(0, P.HELLO, P.Hello().encode()), 1.0)
    assert m.img is None and m.frame_id == 0


def test_hello_slow_only_flag():
    sim = make(slot=SLOT, slow_only=True)
    assert sim.run_session(P.TRIG_BUTTON) == P.RES_OK
    assert sim.cbus[0].hello_rsp.baud == P.BAUD_SLOW and sim.mcu.ep.stats.reverts == 0
    assert not any(f.cmd == P.PING for f in frames(sim, "mcu-rx"))


def test_hello_flags_roundtrip():
    h = P.Hello(1, 256, 7, P.HELLO_F_SLOW_ONLY)
    assert h.encode()[1] == 0x01 and P.Hello.parse(h.encode()) == h


def test_time_refuses_year_2100():
    assert P.Time(4102444800).check() == 0
    assert P.Time(4102444800 - 1800, 60).check() == 0
    assert P.Time(1800000000, 0, 4102444800).check() == 6
    assert P.Time(4102444800 - 7200, 60).check() is None


def test_model_maintenance_cap_not_a_failure():
    sim = make(hold_s=900, hold_dwell=2000.0)
    assert sim.run_session() == P.RES_CAP
    m = sim.mcu
    assert m.fail_count == 0 and m.link_icon == LINK_WAIT and m.next_session_in == 60 * 60


def test_model_hold_refused_in_grace():
    m = McuModel()
    m.start_session(0.0)
    m.state = "GRACE"
    r = m._handle(P.Frame(1, P.HOLD, P.hold_encode(900)), 1.0)
    assert r[0] == P.ERR and P.Err.parse(r[1]).code == P.E_STATE


# --- MCU firmware update (protocol.md 4.14-4.15) -----------------------------------

IMAGE = bytes((i * 37 + 11) & 0xFF for i in range(40000))
FWU = P.fwu_package(IMAGE, 0x00020001)


def linked(**kw):
    m = McuModel(**kw)
    m.start_session(0.0)
    m.state = "LINKED"
    return m


def err_of(r):
    assert r[0] == P.ERR, r
    e = P.Err.parse(r[1])
    return e.code, e.detail


def test_fwu_package_roundtrip():
    b, image, min_boot = P.fwu_parse(FWU)
    assert (b.fw_version, b.length, b.crc32, b.load, min_boot) == (0x00020001, 40000, P.crc32(IMAGE),
                                                                  0x1000, 1)
    assert image == IMAGE and FWU[:4] == b"TH5U" and FWU[24:32] == b"\xff" * 8
    for bad in (FWU[:-4], b"X" + FWU[1:], FWU[:-1] + bytes([FWU[-1] ^ 1])):
        with pytest.raises(ValueError):
            P.fwu_parse(bad)


def test_firmware_session_instead_of_frame():
    mcu = McuModel()
    mcu.load_slot_image(SLOT)
    sim = make(mcu=mcu, fwu=FWU, slot=SLOT, always_begin=True)
    assert sim.run_session(P.TRIG_BUTTON) == P.RES_OK
    cbu = sim.cbus[0]
    assert cbu.hello_rsp.boot_ver == 1 and cbu.fw_result == "staged" and cbu.img_result is None
    rx = frames(sim, "mcu-rx")
    assert P.IMG_BEGIN not in [f.cmd for f in rx]
    data = [f for f in rx if f.cmd == P.IMG_DATA]
    assert len(data) == 40 and P.img_data_parse(data[-1].payload)[0] == 39 * 1020   # image offsets
    assert mcu.fw_staged and mcu.installs == 1 and mcu.fw_version == 0x00020001
    assert mcu.frame_id == 0 and mcu.app == IMAGE           # both slots gone, new app running
    # the next session: HELLO_RSP shows the new version; the same package is HAVE
    sim.run_session()
    assert sim.cbus[-1].hello_rsp.fw_version == 0x00020001 and sim.cbus[-1].fw_result == "have"
    assert not mcu.fw_staged and mcu.installs == 1


def test_firmware_refused_without_bootloader():
    mcu = McuModel()
    mcu.boot_ver = 0
    sim = make(mcu=mcu, fwu=FWU)
    assert sim.run_session() == P.RES_OK
    assert sim.cbus[0].hello_rsp.boot_ver == 0
    assert sim.cbus[0].fw_result == f"refused {P.Err(P.FW_BEGIN, P.E_STATE, P.FWS_NO_BOOT)}"
    assert not mcu.fw_staged and mcu.fw_version == McuModel.FW_VERSION


def test_firmware_requests():
    b = P.FwBegin(0x00020001, len(IMAGE), P.crc32(IMAGE))
    m = linked()
    h = m._handle
    assert err_of(h(P.Frame(1, P.FW_END, P.fw_end_encode(b.crc32)), 0)) == (P.E_STATE, 0)
    assert err_of(h(P.Frame(1, P.FW_BEGIN, P.FwBegin(1, 64, 0, 0x2000).encode()), 0)) == (P.E_BAD_VALUE, 12)
    for n in (60, 66, 50148):
        assert err_of(h(P.Frame(2, P.FW_BEGIN, P.FwBegin(1, n, 0).encode()), 0)) == (P.E_TOO_BIG, 50144)
    assert h(P.Frame(3, P.FW_BEGIN, b.encode()), 0) == (P.ACK, bytes([P.FW_BEGIN, P.IMG_START]))
    assert h(P.Frame(4, P.IMG_DATA, P.img_data_encode(0, IMAGE[:1020])), 0)[0] == P.ACK
    assert err_of(h(P.Frame(5, P.IMG_END, P.img_end_encode(1)), 0)) == (P.E_STATE, 0)
    assert err_of(h(P.Frame(5, P.FW_END, P.fw_end_encode(b.crc32 ^ 1)), 0)) == (P.E_BAD_VALUE, 0)
    assert err_of(h(P.Frame(5, P.FW_END, P.fw_end_encode(b.crc32)), 0)) == (P.E_OFFSET, 1020)
    for off in range(1020, len(IMAGE), 1020):
        assert h(P.Frame(6, P.IMG_DATA, P.img_data_encode(off, IMAGE[off:off + 1020])), 0)[0] == P.ACK
    assert h(P.Frame(7, P.FW_END, P.fw_end_encode(b.crc32)), 0) == (P.ACK, bytes([P.FW_END]))
    assert err_of(h(P.Frame(8, P.IMG_BEGIN, BEGIN.encode()), 0)) == (P.E_STATE, P.FWS_STAGED)
    assert err_of(h(P.Frame(9, P.FW_BEGIN, b.encode()), 0)) == (P.E_STATE, P.FWS_STAGED)


def test_firmware_bad_crc_is_not_staged():
    def fault(d, data, now):
        if d == "c2m" and data[3:4] == bytes([P.IMG_DATA]):
            f = P.decode_frame(data)
            if P.u32(f.payload, 0) == 2040:
                p = bytearray(f.payload)
                p[10] ^= 1
                return P.encode_frame(f.seq, f.cmd, bytes(p))
        return data
    sim = make(fault=fault, fwu=FWU)
    assert sim.run_session() == P.RES_OK
    bad = bytearray(IMAGE)
    bad[2040 + 6] ^= 1
    assert sim.cbus[0].fw_result == f"refused {P.Err(P.FW_END, P.E_CRC, P.crc32(bytes(bad)))}"
    assert not sim.mcu.fw_staged and sim.mcu.installs == 0
