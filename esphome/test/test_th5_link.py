"""The C++ th5_link engine (esphome/components/th5_link/th5_engine.cpp) against the
Python MCU model (tools/th5link/mcumodel.py), in the virtual-time loopback of
tools/th5link/sim.py.

The engine runs in a subprocess (th5_host.cpp, built here with clang++) and is
driven over a line protocol. CppCbu plays the rest of the CBU around it: the BK
boot noise, Wi-Fi and API coming up, Home Assistant calling the three actions,
and the radio going off after begin_shutdown.

    uv run --with pytest pytest esphome/test
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.th5link import proto as P  # noqa: E402
from tools.th5link.fakecbu import BOOT_TEXT  # noqa: E402
from tools.th5link.mcumodel import LINK_OK, McuModel  # noqa: E402
from tools.th5link.sim import Sim, frames  # noqa: E402

COMP = ROOT / "esphome/components/th5_link"
SLOT = (ROOT / "ha/out/frame-C.slot").read_bytes()
BEGIN, PAYLOAD = P.ImgBegin.from_slot(SLOT)
UTC0 = 1793520000  # 2026-11-01T08:00Z at sim time 0

pytestmark = pytest.mark.skipif(not shutil.which("clang++"), reason="clang++ not found")


@pytest.fixture(scope="session")
def host_exe(tmp_path_factory):
    out = tmp_path_factory.mktemp("th5") / "th5_host"
    subprocess.run(["clang++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Wconversion",
                    "-Wno-sign-conversion", "-fsanitize=address,undefined", "-g",
                    f"-I{ROOT / 'firmware/src'}", f"-I{COMP}",
                    str(Path(__file__).with_name("th5_host.cpp")), str(COMP / "th5_engine.cpp"),
                    "-o", str(out)], check=True)
    return out


class Engine:
    """The C++ engine in a subprocess."""

    def __init__(self, exe, **opts):
        args = [str(exe)]
        for k, v in opts.items():
            args += ["--" + k.replace("_", "-"), str(int(v))]
        self.p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.next_ms: int | None = None

    def cmd(self, name, ms, *args) -> list[str]:
        self.p.stdin.write(" ".join([name, str(ms), *map(str, args)]) + "\n")
        self.p.stdin.flush()
        events = []
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError(f"th5_host died (rc={self.p.poll()})")
            line = line.rstrip("\n")
            if line == ".":
                break
            if line.startswith("next "):
                v = int(line[5:])
                self.next_ms = None if v < 0 else v
            else:
                events.append(line)
        return events

    def close(self):
        self.p.stdin.close()
        rc = self.p.wait(5)
        assert rc == 0, f"th5_host exit {rc} (sanitizer?)"


@dataclass
class Ha:
    """What Home Assistant and the network do in one session."""
    wifi_s: float | None = 1.0        # boot -> Wi-Fi up; None = never
    api_s: float | None = 0.5         # Wi-Fi up -> API client subscribed; None = never
    present: bool = True              # calls th5_config
    react_s: float = 0.05             # HA's delay between seeing a change and calling
    tags: dict = field(default_factory=lambda: {P.CFG_MODE: 1, P.CFG_SYNC_MIN: 60, P.CFG_CLOCK_MIN: 1,
                                                P.CFG_CLOCK_24H: 1, P.CFG_FULL_N: 30, P.CFG_T_OFF: 0,
                                                P.CFG_RH_OFF: 0, P.CFG_FLAGS: 0})
    history_ack: bool = False
    utc: int | None = None            # None: UTC0 + now
    slot: bytes | None = None
    chunk: int = 3072                 # raw bytes per th5_frame (≤ 4 KB of base64)
    radio_off_s: float = 0.3          # begin_shutdown -> radio off
    call_done: bool = True
    after_upload: list = field(default_factory=list)  # (offset, bytes) th5_frame calls after the upload
    fwu: bytes | None = None          # an MCU firmware package: th5_firmware instead of th5_frame
    fw_chunk: int = 4096              # raw bytes per th5_firmware (as the integration)
    script: list = field(default_factory=list)  # (t from boot, shim cmd, *args): extra HA/platform events


class CppCbu:
    """The sim.py runner interface around the C++ engine."""

    def __init__(self, exe, ha: Ha, **opts):
        self.eng = Engine(exe, **opts)
        self.ha = ha
        self.ep = types.SimpleNamespace(trace=None, stats=None)
        self.baud = P.BAUD_SLOW
        self.tx = bytearray()
        self.events: list[tuple[float, str]] = []
        self.sched: list[tuple[float, str, tuple]] = []
        self.responses: dict[int, tuple[bool, str]] = {}
        self.session: str | None = None
        self.hello_flags = 0
        self.finished: bool | None = None
        self.shutdowns = 0
        self.forgets = 0
        self.syncs = 0
        self.frame_calls = 0
        self.fw_calls = 0
        self.urgent = None
        self.now = 0.0

    # --- runner interface ---------------------------------------------------
    def start(self, now):
        self.now = now
        self.tx += BOOT_TEXT                  # BK ROM / LibreTiny boot text: garbage to the MCU
        self.at(now + 0.3, "start")
        for t, name, *args in self.ha.script:
            self.at(now + t, name, *args)
        if self.ha.wifi_s is not None:
            self.at(now + 0.3 + self.ha.wifi_s, "wifi")
            if self.ha.api_s is not None:
                self.at(now + 0.3 + self.ha.wifi_s + self.ha.api_s, "api", 1, -58)

    def feed(self, data, now):
        self.now = now
        self._run("feed", now, data.hex())

    def poll(self, now):
        self.now = now
        due = [s for s in self.sched if s[0] <= now + 1e-9]
        self.sched = [s for s in self.sched if s[0] > now + 1e-9]
        for _t, name, args in sorted(due, key=lambda s: s[0]):
            if name == "ha_frames":
                self._next_frame(now, *args)
                continue
            if name == "ha_fw":
                self._next_fw(now, *args)
                continue
            self._run(name, now, *args)
        self._run("tick", now)

    def take_tx(self):
        b = bytes(self.tx)
        self.tx.clear()
        return b

    def tx_done(self, now):
        pass

    def next_deadline(self):
        ts = [s[0] for s in self.sched]
        if self.eng.next_ms is not None:
            ts.append(max(self.eng.next_ms / 1000.0, self.now + 0.001))
        return min(ts) if ts else None

    # --- plumbing -----------------------------------------------------------
    def at(self, t, name, *args):
        self.sched.append((t, name, args))

    def _run(self, name, now, *args):
        ms = round(now * 1000)
        for e in self.eng.cmd(name, ms, *args):
            self.events.append((now, e))
            self._on_event(e, now)

    def _on_event(self, e, now):
        kind, _, rest = e.partition(" ")
        if kind == "tx":
            raw = bytes.fromhex(rest)
            self.tx += raw
            if self.ep.trace:
                self.ep.trace("tx", P.decode_frame(raw), now)
        elif kind == "baud":
            self.baud = int(rest)
        elif kind == "hello":
            self.session, flags = rest.split(" ")
            self.hello_flags = int(flags)
        elif kind == "shutdown":
            self.shutdowns += 1
            self.urgent = rest == "urgent"
            self.at(now + self.ha.radio_off_s, "radiooff")
        elif kind == "sync":
            self.syncs += 1
        elif kind == "forget":
            self.forgets += 1
        elif kind == "finished":
            self.finished = rest == "1"
        elif kind == "respond":
            call, ok, msg = (rest.split(" ", 2) + [""])[:3]
            self.responses[int(call)] = (ok == "1", msg)
            self._ha_next(int(call), ok == "1", now)
        elif kind == "log":
            pass
        # API up: HA sees the session sensor and starts
        if e.startswith("tx ") and self.ha.present:
            f = P.decode_frame(bytes.fromhex(rest))
            if f.cmd == P.NET and f.payload[0] == P.NET_API_UP:
                self._ha_config(now + self.ha.react_s)

    # --- Home Assistant -------------------------------------------------------
    def _ha_config(self, t):
        utc = self.ha.utc if self.ha.utc is not None else UTC0 + int(t)
        tags = ",".join(f"{k}:{v}" for k, v in sorted(self.ha.tags.items())) or "-"
        self.at(t, "config", 1, utc, 60, 1806195600, 120, tags, int(self.ha.history_ack))

    def _ha_next(self, call, ok, now):
        if call == 1:                         # th5_config answered
            fid = int(self.session.split(";")[2], 16)
            if self.ha.fwu is not None:
                self.at(now + self.ha.react_s, "ha_fw", 0)
            elif self.ha.slot is not None and P.u64(self.ha.slot, 12) != fid:
                self.at(now + self.ha.react_s, "ha_frames", 0)
            elif self.ha.call_done:
                self.at(now + self.ha.react_s, "done", 999)
        elif 100 <= call < 999 and ok:
            self.at(now + 0.005, "ha_frames", (call - 100 + 1) * self.ha.chunk)
        elif 2000 <= call < 3000:             # th5_firmware: next chunk, or th5_done after a refusal
            if ok:
                self.at(now + 0.005, "ha_fw", (call - 2000 + 1) * self.ha.fw_chunk)
            elif self.ha.call_done:
                self.at(now + 0.005, "done", 999)

    def _next_fw(self, now, off):
        pkg = self.ha.fwu
        if off >= len(pkg):
            if self.ha.call_done:
                self._run("done", now, 999)
            return
        self.fw_calls += 1
        data = base64.b64encode(pkg[off:off + self.ha.fw_chunk]).decode()
        self._run("firmware", now, 2000 + off // self.ha.fw_chunk, off, data)

    def _next_frame(self, now, off):
        slot = self.ha.slot
        n = P.u32(slot, 20) + P.FRAME_HDR_BYTES
        if off >= n:
            for i, (o, data) in enumerate(self.ha.after_upload):   # call ids 1000+: no follow-up
                self._run("frame", now, 1000 + i, o, base64.b64encode(data).decode())
            if self.ha.call_done:
                self._run("done", now, 999)
            return
        data = base64.b64encode(slot[off:off + self.ha.chunk]).decode()
        self.frame_calls += 1
        self._run("frame", now, 100 + off // self.ha.chunk, off, data)


def run(host_exe, ha=None, mcu=None, fault=None, trigger=P.TRIG_BUTTON, bench=False, limit=1000.0, **opts):
    ha = ha or Ha()
    mcu = mcu or McuModel()
    cbus: list[CppCbu] = []

    def factory():
        c = CppCbu(host_exe, ha, **opts)
        cbus.append(c)
        return c
    sim = Sim(mcu, factory, fault=fault)
    result = sim.run_session(trigger, bench=bench, limit=limit)
    for c in cbus:
        c.eng.close()
    return sim, cbus, result


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


# --- helpers in C++ ---------------------------------------------------------------

def test_crc32_and_base64_helpers(host_exe):
    e = Engine(host_exe)
    assert e.cmd("crc32", 0, b"123456789".hex()) == ["crc32 cbf43926"]
    assert e.cmd("crc32", 0, PAYLOAD.hex()) == [f"crc32 {BEGIN.crc32:08x}"]
    assert e.cmd("b64", 0, "SGVsbG8=") == ["b64 " + b"Hello".hex()]
    assert e.cmd("b64", 0, "SGVsbG8") == ["b64 " + b"Hello".hex()]
    assert e.cmd("b64", 0, "SGV$bG8=") == ["b64 ERR"]
    e.close()


# --- sessions -----------------------------------------------------------------------

def test_hello_bytes_match_the_worked_example(host_exe):
    sim, cbus, res = run(host_exe, Ha(slot=SLOT), fast=1)
    first = [bytes.fromhex(e[3:]) for _, e in cbus[0].events if e.startswith("tx ")][0]
    # protocol.md 7 (a fast_baud session): HELLO seq 0, proto_max 1, flags 0, rx_max 256, fw 1.0.0
    assert first.hex(" ") == "a5 01 00 01 08 00 01 00 00 01 00 00 01 00 de 21"


def test_hello_slow_only_flag_when_fast_baud_off(host_exe):
    sim, cbus, res = run(host_exe, Ha(slot=SLOT))
    hello = frames(sim, "mcu-rx")[0]
    assert hello.cmd == P.HELLO and hello.payload[1] == P.HELLO_F_SLOW_ONLY
    # the MCU honours it: offers 115200, no probe, nothing to revert
    assert res == P.RES_OK and sim.mcu.ep.stats.reverts == 0
    assert P.PING not in [f.cmd for f in frames(sim, "mcu-rx")]


def test_mcu_ignoring_slow_only_still_converges(host_exe):
    # an MCU without the flag (strip it on the wire): it offers 500 k, the CBU
    # stays silent at 115200 until the MCU's probe lapses (PH_SLOW_WAIT)
    def fault(d, data, now):
        if d == "c2m":
            try:
                f = P.decode_frame(data)
            except ValueError:
                return data
            if f.cmd == P.HELLO:
                return P.encode_frame(f.seq, f.cmd, f.payload[:1] + b"\x00" + f.payload[2:])
        return data
    sim, cbus, res = run(host_exe, Ha(slot=SLOT), fault=fault)
    assert res == P.RES_OK and bytes(sim.mcu.active_slot().data) == PAYLOAD
    assert sim.mcu.ep.stats.reverts == 1 and cbus[0].baud == P.BAUD_SLOW


def test_full_session_with_frame_at_115200(host_exe):
    sim, cbus, res = run(host_exe, Ha(slot=SLOT))
    assert res == P.RES_OK
    m, c = sim.mcu, cbus[0]
    assert m.frame_id == BEGIN.frame_id and bytes(m.active_slot().data) == PAYLOAD
    assert m.time.offset_min == 60 and m.time.next_offset_min == 120
    assert m.time.utc >= UTC0
    assert m.settings.values[P.CFG_SYNC_MIN] == 60 and m.settings_writes == 0
    assert m.link_icon == LINK_OK and m.refresh == "full"
    assert c.finished is True and c.shutdowns == 1 and c.syncs == 1
    assert c.responses[1] == (True, "") and c.responses[999][0] is True
    assert all(ok for ok, _ in c.responses.values())
    # session text sensor: nonce;units;frame_id;settings_crc;boot;mcu_fw;boot_ver
    nonce, units, fid, crc, boot, mcu_fw, boot_ver = c.session.split(";")
    assert len(nonce) == 8 and units == "C" and fid == "0" * 16 and crc == "fafe77c2"
    assert boot == "00000007"                  # the shim's boot counter
    assert mcu_fw == "00010000" and boot_ver == "01"
    assert c.urgent is False
    # fast_baud off: HELLO says SLOW_ONLY, so the MCU never left 115200
    assert m.ep.stats.reverts == 0 and c.baud == P.BAUD_SLOW
    rx = frames(sim, "mcu-rx")
    cmds = [f.cmd for f in rx]
    assert cmds[:2] == [P.HELLO, P.NET] and P.PING not in cmds
    assert [f.payload[0] for f in rx if f.cmd == P.NET] == [P.NET_WIFI_CONNECTING, P.NET_WIFI_UP, P.NET_API_UP,
                                                             P.NET_HA_CONFIG]
    assert cmds[-1] == P.BYE and rx[-1].payload == bytes([P.BYE_DONE])
    assert sum(f.cmd == P.IMG_DATA for f in rx) == 25
    # the IMG transfer happens only after the radio is off (design 3 step 8)
    t_radio = next(t for t, e in c.events if e.startswith("tx ") and
                   P.decode_frame(bytes.fromhex(e[3:])).cmd == P.IMG_BEGIN)
    t_off = max(t for t, name in [(t, e) for t, e in c.events] if name == "shutdown")
    assert t_radio >= t_off + Ha().radio_off_s - 1e-6
    # no retries needed on a clean line
    assert sum(1 for f in rx if f.cmd == P.HELLO) == 1


def test_full_session_fast_baud(host_exe):
    sim, cbus, res = run(host_exe, Ha(slot=SLOT), fast=1)
    assert res == P.RES_OK
    m, c = sim.mcu, cbus[0]
    assert bytes(m.active_slot().data) == PAYLOAD
    # (the MCU model resets its endpoint to 115200 at power-off, so look at reverts)
    assert c.baud == P.BAUD_FAST and m.ep.stats.reverts == 0
    rx = frames(sim, "mcu-rx")
    assert [f.cmd for f in rx[:3]] == [P.HELLO, P.PING, P.NET]
    assert rx[1].payload == bytes([0x55, 0xAA, 0x00, 0xFF])


def test_same_frame_is_not_resent(host_exe):
    mcu = McuModel()
    mcu.load_slot_image(SLOT)
    sim, cbus, res = run(host_exe, Ha(slot=SLOT), mcu=mcu)
    assert res == P.RES_OK
    assert cbus[0].frame_calls == 0
    assert not any(f.cmd in (P.IMG_BEGIN, P.IMG_DATA) for f in frames(sim, "mcu-rx"))


def test_settings_change_is_applied(host_exe):
    ha = Ha()
    ha.tags = {P.CFG_SYNC_MIN: 30, P.CFG_T_OFF: -50, P.CFG_FLAGS: P.CFG_FLAG_BADGE}
    sim, cbus, res = run(host_exe, ha)
    assert res == P.RES_OK
    v = sim.mcu.settings.values
    assert v[P.CFG_SYNC_MIN] == 30 and v[P.CFG_T_OFF] == -50 and v[P.CFG_FLAGS] == 1
    assert v[P.CFG_MODE] == 1                  # absent tags keep their value (delta)
    assert sim.mcu.next_session_in == 30 * 60


def test_config_error_is_reported_to_ha(host_exe):
    ha = Ha(utc=1000)                          # before 2025: TIME BAD_VALUE detail 0
    sim, cbus, res = run(host_exe, ha)
    ok, msg = cbus[0].responses[1]
    assert not ok and "TIME refused: BAD_VALUE, detail 0" in msg
    assert sim.mcu.time is None
    assert res == P.RES_OK                     # HA still calls th5_done


def test_config_value_out_of_range(host_exe):
    ha = Ha()
    ha.tags = {P.CFG_SYNC_MIN: 5}
    sim, cbus, res = run(host_exe, ha)
    ok, msg = cbus[0].responses[1]
    assert not ok and "CONFIG refused: BAD_VALUE, detail 2" in msg


def test_no_ha_ends_with_bye_no_ha(host_exe):
    sim, cbus, res = run(host_exe, Ha(present=False))
    assert res == P.RES_NET_FAIL
    rx = frames(sim, "mcu-rx")
    assert rx[-1].cmd == P.BYE and rx[-1].payload == bytes([P.BYE_NO_HA])
    t_api = next(t for t, e in cbus[0].events if e == "shutdown")
    assert abs(t_api - (0.3 + 1.0 + 0.5 + 3.0)) < 0.05
    assert sim.mcu.time is None


def test_wifi_failure_reports_net_fail(host_exe):
    sim, cbus, res = run(host_exe, Ha(wifi_s=None), wifi_timeout=4000)
    assert res == P.RES_NET_FAIL
    rx = frames(sim, "mcu-rx")
    nets = [f.payload[0] for f in rx if f.cmd == P.NET]
    assert nets == [P.NET_WIFI_CONNECTING, P.NET_FAIL_WIFI]
    assert rx[-1].cmd == P.BYE and rx[-1].payload == bytes([P.BYE_NET_FAIL])
    assert sim.mcu.net.state == P.NET_FAIL_WIFI
    assert cbus[0].urgent is True              # skip the API wait (2 s grace)
    assert cbus[0].syncs == 1                  # a cleared AP pin is flushed before BYE
    # BYE came inside the MCU's 2 s grace
    assert sim.mcu.history[-1][0] < 0.3 + 4.0 + 2.0


def test_api_failure(host_exe):
    sim, cbus, res = run(host_exe, Ha(api_s=None), api_timeout=3000)
    assert res == P.RES_NET_FAIL
    assert [f.payload[0] for f in frames(sim, "mcu-rx") if f.cmd == P.NET][-1] == P.NET_FAIL_API


def test_maintenance_holds_and_skips_done(host_exe):
    ha = Ha()
    ha.tags = {P.CFG_FLAGS: P.CFG_FLAG_MAINTENANCE}
    sim, cbus, res = run(host_exe, ha, limit=60.0)
    m, c = sim.mcu, cbus[0]
    # the CBU stays on until the MCU's raised cap (t0 + 900 s) ends the session
    assert res == P.RES_CAP and m.history[-1][0] >= 900 - 1
    assert c.responses[999] == (True, "maintenance: staying on")
    assert c.shutdowns == 0
    assert P.HOLD in [f.cmd for f in frames(sim, "mcu-rx")]


def test_bench_pushes_frame_and_stays_on(host_exe):
    sim, cbus, res = run(host_exe, Ha(slot=SLOT), bench=True, limit=30.0)
    m, c = sim.mcu, cbus[0]
    assert res == P.RES_CAP and m.history[-1][0] >= 900 - 1   # bench: on until the 900 s cap
    assert c.hello_flags & P.F_BENCH
    assert m.frame_id == BEGIN.frame_id        # committed with the radio on
    assert c.responses[999] == (True, "")      # th5_done answered after IMG_END
    assert c.shutdowns == 0


def test_forget(host_exe):
    mcu = McuModel()
    mcu.forget_pending = True
    sim, cbus, res = run(host_exe, Ha(), mcu=mcu)
    c = cbus[0]
    assert res == P.RES_OK and not mcu.forget_pending
    assert c.forgets == 1 and c.shutdowns == 1
    rx = frames(sim, "mcu-rx")
    assert rx[-1].cmd == P.BYE and rx[-1].payload == bytes([P.BYE_FORGOT])
    assert not any(f.cmd == P.NET for f in rx)  # no Wi-Fi session after FORGET


def test_lost_hello_rsp_is_retried(host_exe):
    sim, cbus, res = run(host_exe, Ha(slot=SLOT), fast=1, fault=drop_nth("m2c", P.HELLO_RSP))
    assert res == P.RES_OK
    assert sum(f.cmd == P.HELLO for f in frames(sim, "mcu-rx")) == 2
    assert bytes(sim.mcu.active_slot().data) == PAYLOAD


def test_lost_probe_ack_converges(host_exe):
    sim, cbus, res = run(host_exe, Ha(slot=SLOT), fast=1, fault=drop_nth("m2c", P.ACK, acked=P.PING))
    assert res == P.RES_OK
    assert sim.mcu.ep.stats.reverts == 1 and cbus[0].baud == P.BAUD_SLOW
    assert bytes(sim.mcu.active_slot().data) == PAYLOAD


def test_lost_img_data_ack_is_reacked_not_rewritten(host_exe):
    sim, cbus, res = run(host_exe, Ha(slot=SLOT), fast=1, fault=drop_nth("m2c", P.ACK, n=8, acked=P.IMG_DATA))
    assert res == P.RES_OK
    assert sim.mcu.ep.stats.dup_requests >= 1
    assert bytes(sim.mcu.active_slot().data) == PAYLOAD
    assert sim.mcu.ep.stats.reverts == 1 and cbus[0].baud == P.BAUD_SLOW  # R1a/R1b


def test_bad_fast_link_reverts_both(host_exe):
    def fault(d, data, now):
        # everything at 500 k is corrupted after the probe (flip a payload byte)
        if len(data) > 8 and sim_ref[0].mcu.ep.baud == P.BAUD_FAST and data[3] in (P.NET, P.ACK):
            b = bytearray(data)
            b[-3] ^= 0xFF
            return bytes(b)
        return data
    sim_ref = [None]
    mcu = McuModel()

    class S(Sim):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            sim_ref[0] = self
    import tools.th5link.sim as simmod
    orig = simmod.Sim
    globals()["Sim"] = S
    try:
        sim, cbus, res = run(host_exe, Ha(), mcu=mcu, fast=1, fault=fault)
    finally:
        globals()["Sim"] = orig
    assert res == P.RES_OK
    assert sim.mcu.ep.stats.reverts == 1 and cbus[0].baud == P.BAUD_SLOW


def test_corrupt_frame_upload_is_refused(host_exe):
    bad = bytearray(SLOT)
    bad[P.FRAME_HDR_BYTES + 100] ^= 1
    sim, cbus, res = run(host_exe, Ha(slot=bytes(bad)))
    c = cbus[0]
    fails = [m for ok, m in c.responses.values() if not ok]
    assert fails and "payload CRC" in fails[0]
    assert sim.mcu.frame_id == 0               # nothing sent to the MCU
    assert not any(f.cmd == P.IMG_BEGIN for f in frames(sim, "mcu-rx"))


def test_completed_frame_cannot_be_rewritten(host_exe):
    # after the upload is complete and CRC-checked, a different write at offset > 0
    # (here: the header's bpp/flags/crc bytes) is refused; a byte-identical retry is fine
    hdr = bytearray(SLOT[:P.FRAME_HDR_BYTES])
    hdr[5] ^= 0x03
    hdr[24:28] = b"\0\0\0\0"
    ha = Ha(slot=SLOT, after_upload=[(4, bytes(hdr[4:])),
                                     (P.FRAME_HDR_BYTES, SLOT[P.FRAME_HDR_BYTES:P.FRAME_HDR_BYTES + 64])])
    sim, cbus, res = run(host_exe, ha)
    c = cbus[0]
    assert c.responses[1000] == (False, "frame already complete: restart the upload at offset 0")
    assert c.responses[1001] == (True, "")
    assert res == P.RES_OK
    begin = next(f for f in frames(sim, "mcu-rx") if f.cmd == P.IMG_BEGIN)
    assert begin.payload == BEGIN.encode()      # header fields of the checked frame
    assert sim.mcu.frame_id == BEGIN.frame_id and bytes(sim.mcu.active_slot().data) == PAYLOAD


def test_tiny_mcu_rx_max_ends_with_bye_link(host_exe):
    # HELLO_RSP announcing rx_max 16: nothing but BYE fits; end at once, no loop
    def fault(d, data, now):
        if d == "m2c":
            try:
                f = P.decode_frame(data)
            except ValueError:
                return data
            if f.cmd == P.HELLO_RSP:
                p = bytearray(f.payload)
                p[6:8] = (16).to_bytes(2, "little")
                return P.encode_frame(f.seq, f.cmd, bytes(p))
        return data
    sim, cbus, res = run(host_exe, Ha(slot=SLOT), fault=fault)
    c = cbus[0]
    rx = frames(sim, "mcu-rx")
    assert [f.cmd for f in rx] == [P.HELLO, P.BYE]
    assert rx[-1].payload == bytes([P.BYE_LINK])
    assert c.finished is True and c.shutdowns == 1
    assert sum(1 for _, e in c.events if e.startswith("log ")) < 10


# --- second review ---------------------------------------------------------------

def image_events(c):
    return [e[6:] for _, e in c.events if e.startswith("image ")]


def test_soft_deadline_pushes_frame_then_bye_no_ha(host_exe):
    # HA uploads the frame but never calls th5_done
    sim, cbus, res = run(host_exe, Ha(slot=SLOT, call_done=False))
    m, c = sim.mcu, cbus[0]
    rx = frames(sim, "mcu-rx")
    assert rx[-1].cmd == P.BYE and rx[-1].payload == bytes([P.BYE_NO_HA])
    assert m.frame_id == BEGIN.frame_id        # the complete frame was still pushed
    assert image_events(c) == ["1 frame committed"]
    t_sd = next(t for t, e in c.events if e.startswith("shutdown"))
    assert abs(t_sd - (0.3 + 25.0)) < 0.05
    assert m.history[-1][0] < 30.0             # well before the MCU's 45 s cap
    assert res == P.RES_NET_FAIL               # BYE NO_HA: HA never finished


def test_soft_deadline_without_frame(host_exe):
    sim, cbus, res = run(host_exe, Ha(call_done=False), session_timeout=8000)
    rx = frames(sim, "mcu-rx")
    assert rx[-1].payload == bytes([P.BYE_NO_HA]) and not any(f.cmd == P.IMG_BEGIN for f in rx)
    assert sim.mcu.history[-1][0] < 0.3 + 8.0 + 1.0


def test_maintenance_off_lets_done_end_the_session(host_exe):
    ha = Ha()
    ha.tags = {P.CFG_FLAGS: P.CFG_FLAG_MAINTENANCE}
    ha.script = [(10.0, "config", 2, UTC0 + 10, 60, 1806195600, 120, f"{P.CFG_FLAGS}:0"),
                 (11.0, "done", 5000)]
    sim, cbus, res = run(host_exe, ha, limit=60.0)
    c = cbus[0]
    assert c.responses[999] == (True, "maintenance: staying on")
    assert c.responses[2] == (True, "") and c.responses[5000] == (True, "")
    rx = frames(sim, "mcu-rx")
    assert P.HOLD in [f.cmd for f in rx]
    assert rx[-1].cmd == P.BYE and rx[-1].payload == bytes([P.BYE_DONE])
    assert res == P.RES_OK and sim.mcu.history[-1][0] < 15.0


def test_ota_sends_hold_and_stops_timers(host_exe):
    ha = Ha(call_done=False)
    ha.script = [(2.2, "ota")]
    sim, cbus, res = run(host_exe, ha, limit=60.0)
    c = cbus[0]
    rx = frames(sim, "mcu-rx")
    cmds = [f.cmd for f in rx]
    i = next(i for i, f in enumerate(rx) if f.cmd == P.NET and f.payload[0] == P.NET_OTA)
    assert P.HOLD in cmds[i:] and P.hold_parse(rx[cmds.index(P.HOLD, i)].payload) == 900
    assert c.shutdowns == 0                    # no NO_HA, no soft deadline
    assert res == P.RES_CAP and sim.mcu.history[-1][0] >= 900 - 1


@pytest.mark.parametrize("off,val,msg", [
    (5, 3, "bpp 3 (1 or 2)"),
    (6, 1, "encoding 1 (0 raw)"),
    (7, 0x82, "flags 0x82"),
    (20, (12480).to_bytes(4, "little"), "length 12480 != 12480 x bpp"),
    (12, bytes(8), "frame_id 0"),
])
def test_bad_slot_header_is_refused_by_th5_frame(host_exe, off, val, msg):
    bad = bytearray(SLOT)
    if isinstance(val, int):
        bad[off] = val
    else:
        bad[off:off + len(val)] = val
    mcu = McuModel()
    if off == 12:                              # HA uploads only when the ids differ
        mcu.load_slot_image(SLOT)
    sim, cbus, res = run(host_exe, Ha(slot=bytes(bad)), mcu=mcu, session_timeout=6000)
    ok, m = cbus[0].responses[100]
    assert not ok and msg in m
    assert not any(f.cmd == P.IMG_BEGIN for f in frames(sim, "mcu-rx"))


def test_push_failure_after_done_is_reported(host_exe):
    # flip a byte inside one IMG_DATA on the wire (CRC re-sealed): IMG_END fails
    n = [0]

    def fault(d, data, now):
        if d != "c2m":
            return data
        try:
            f = P.decode_frame(data)
        except ValueError:
            return data
        if f.cmd != P.IMG_DATA:
            return data
        n[0] += 1
        if n[0] != 3:
            return data
        p = bytearray(f.payload)
        p[10] ^= 0xFF
        return P.encode_frame(f.seq, f.cmd, bytes(p))
    sim, cbus, res = run(host_exe, Ha(slot=SLOT), fault=fault)
    c = cbus[0]
    assert c.responses[999] == (True, "")      # th5_done answers before the push
    ev = image_events(c)
    assert len(ev) == 1 and ev[0].startswith("0 IMG_END refused: CRC")
    assert sim.mcu.frame_id == 0               # next HELLO_RSP frame_id is the truth


def test_rejected_config_restarts_ha_window(host_exe):
    ha = Ha(present=False, call_done=False)
    api_up = 0.3 + 1.0 + 0.5
    # an engine-rejected th5_config (offset does not fit i16) 2 s after the API
    ha.script = [(api_up + 2.0, "config", 3, UTC0, 40000, 0, 0, "-")]
    sim, cbus, res = run(host_exe, ha)
    c = cbus[0]
    assert c.responses[3] == (False, "offset does not fit i16")
    t_sd = next(t for t, e in c.events if e.startswith("shutdown"))
    assert abs(t_sd - (api_up + 2.0 + 3.0)) < 0.05


def test_ha_activity_from_glue_restarts_ha_window(host_exe):
    ha = Ha(present=False, call_done=False)
    api_up = 0.3 + 1.0 + 0.5
    ha.script = [(api_up + 2.5, "haact")]      # e.g. th5_config rejected as bad JSON
    sim, cbus, res = run(host_exe, ha)
    t_sd = next(t for t, e in cbus[0].events if e.startswith("shutdown"))
    assert abs(t_sd - (api_up + 2.5 + 3.0)) < 0.05


def test_pending_done_is_answered_when_forget_wins(host_exe):
    mcu = McuModel()
    mcu.forget_pending = True
    ha = Ha(present=False)
    ha.script = [(0.3, "done", 5000)]         # right after start, before HELLO_RSP
    sim, cbus, res = run(host_exe, ha, mcu=mcu)
    c = cbus[0]
    assert c.responses[5000] == (False, "session closing (BYE reason 4)")
    assert frames(sim, "mcu-rx")[-1].payload == bytes([P.BYE_FORGOT])


def test_stalled_loop_keeps_a_split_frame(host_exe):
    # The loop reads half a HELLO_RSP, then stalls 60 ms; the rest was in the UART
    # ring all along and arrives in one feed with the late timestamp.
    eng = Engine(host_exe)
    try:
        ev = eng.cmd("start", 0)
        hello = P.decode_frame(bytes.fromhex(next(e for e in ev if e.startswith("tx "))[3:]))
        rsp = P.encode_frame(hello.seq, P.HELLO_RSP, P.HelloRsp(baud=P.BAUD_SLOW, rx_max=1024).encode())
        eng.cmd("feed", 10, rsp[:20].hex())
        ev = eng.cmd("feed", 70, rsp[20:].hex())
        ev += eng.cmd("tick", 70)
        assert any(e.startswith("hello ") for e in ev), ev
        # a genuinely stale partial frame still expires on a tick with no bytes
        ev = eng.cmd("feed", 100, "a501")
        eng.cmd("tick", 130)
        assert eng.next_ms is None or eng.next_ms > 130
    finally:
        eng.close()


# --- MCU firmware (th5_firmware, protocol.md 4.14-4.15 and 6 item 5) -----------------

IMAGE = bytes((i * 37 + 11) & 0xFF for i in range(40000))
FWU = P.fwu_package(IMAGE, 0x00020001)


def fw_events(c):
    return [e[3:] for _, e in c.events if e.startswith("fw ")]


def logs(c):
    return [e[6:] for _, e in c.events if e.startswith("log ")]


def test_firmware_session_replaces_the_frame(host_exe):
    mcu = McuModel()
    mcu.load_slot_image(SLOT)
    sim, cbus, res = run(host_exe, Ha(fwu=FWU, slot=SLOT), mcu=mcu)
    c = cbus[0]
    assert res == P.RES_OK and c.finished is True
    assert c.fw_calls == 10 and all(ok for ok, _ in c.responses.values())    # 4 KB chunks
    assert c.session.split(";")[5:] == ["00010000", "01"]
    rx = frames(sim, "mcu-rx")
    cmds = [f.cmd for f in rx]
    assert P.IMG_BEGIN not in cmds
    i = cmds.index(P.FW_BEGIN)
    assert P.FwBegin.parse(rx[i].payload) == P.FwBegin(0x00020001, len(IMAGE), P.crc32(IMAGE))
    data = [P.img_data_parse(f.payload) for f in rx if f.cmd == P.IMG_DATA]
    assert [off for off, _ in data] == [k * 1020 for k in range(40)]          # image offsets
    assert b"".join(d for _, d in data) == IMAGE
    assert cmds[-2:] == [P.FW_END, P.BYE] and rx[-1].payload == bytes([P.BYE_DONE])
    assert P.fw_end_parse(rx[-2].payload) == P.crc32(IMAGE)
    # with the radio off (after the shutdown), as the frame push
    t_begin = next(t for t, e in c.events if e.startswith("tx ") and
                   P.decode_frame(bytes.fromhex(e[3:])).cmd == P.FW_BEGIN)
    t_sd = next(t for t, e in c.events if e == "shutdown")
    assert t_begin >= t_sd + Ha().radio_off_s - 1e-6
    assert fw_events(c) == ["1 staged; the MCU installs it after power-off"]
    assert mcu.fw_staged and mcu.installs == 1 and mcu.fw_version == 0x00020001
    assert mcu.frame_id == 0
    log = logs(c)
    assert any("MCU firmware upload: 40032 B buffer, free heap 200000" in m for m in log)
    assert any(m.startswith("MCU firmware 0.2.1 buffered: 40000 bytes") for m in log)
    assert any(m == "MCU firmware 0.2.1: staged; the MCU installs it after power-off" for m in log)
    # the next session reports the new version, and the same package is HAVE
    sim2, cbus2, res2 = run(host_exe, Ha(fwu=FWU), mcu=mcu)
    c2 = cbus2[0]
    assert res2 == P.RES_OK and c2.session.split(";")[5] == "00020001"
    assert fw_events(c2) == ["1 the MCU already runs this firmware"]
    assert not any(f.cmd in (P.IMG_DATA, P.FW_END) for f in frames(sim2, "mcu-rx"))
    assert mcu.installs == 1


def test_firmware_refused_without_bootloader(host_exe):
    mcu = McuModel()
    mcu.boot_ver = 0
    sim, cbus, res = run(host_exe, Ha(fwu=FWU), mcu=mcu)
    c = cbus[0]
    assert c.session.split(";")[6] == "00"
    assert c.responses[2000] == (False, "the MCU has no bootloader (boot_ver 0): install it over SWD")
    assert c.fw_calls == 1 and res == P.RES_OK         # th5_done still ends the session
    assert not any(f.cmd == P.FW_BEGIN for f in frames(sim, "mcu-rx"))


def test_firmware_crc_refused_at_fw_end(host_exe):
    n = [0]

    def fault(d, data, now):
        if d != "c2m":
            return data
        try:
            f = P.decode_frame(data)
        except ValueError:
            return data
        if f.cmd != P.IMG_DATA:
            return data
        n[0] += 1
        if n[0] != 5:
            return data
        p = bytearray(f.payload)
        p[10] ^= 0xFF
        return P.encode_frame(f.seq, f.cmd, bytes(p))
    sim, cbus, res = run(host_exe, Ha(fwu=FWU), fault=fault)
    c = cbus[0]
    assert c.responses[999] == (True, "")             # th5_done answers before the push
    ev = fw_events(c)
    assert len(ev) == 1 and ev[0].startswith("0 FW_END refused: CRC, detail ")
    assert not sim.mcu.fw_staged and sim.mcu.installs == 0
    assert frames(sim, "mcu-rx")[-1].payload == bytes([P.BYE_DONE])


def test_firmware_lost_ack_is_retried(host_exe):
    sim, cbus, res = run(host_exe, Ha(fwu=FWU), fault=drop_nth("m2c", P.ACK, n=1, acked=P.FW_BEGIN))
    assert res == P.RES_OK and sim.mcu.fw_staged
    assert sim.mcu.ep.stats.dup_requests == 1       # FW_BEGIN re-ACKed, not re-applied
    assert fw_events(cbus[0]) == ["1 staged; the MCU installs it after power-off"]


def test_firmware_soft_deadline_without_time_left_is_not_sent(host_exe):
    # HA uploads the package but never calls th5_done; with a 38 s soft deadline
    # the push (~5 s at 115200) would end too close to the MCU's 45 s cap
    sim, cbus, res = run(host_exe, Ha(fwu=FWU, call_done=False), session_timeout=38000)
    c = cbus[0]
    ev = fw_events(c)
    assert len(ev) == 1 and ev[0].startswith("0 not sent: 40") and "the push needs ~" in ev[0]
    rx = frames(sim, "mcu-rx")
    assert not any(f.cmd == P.FW_BEGIN for f in rx) and rx[-1].payload == bytes([P.BYE_NO_HA])
    assert sim.mcu.history[-1][0] < 45.0 and res == P.RES_NET_FAIL


def test_firmware_soft_deadline_with_time_left_is_sent(host_exe):
    sim, cbus, res = run(host_exe, Ha(fwu=FWU, call_done=False))           # default 25 s
    assert fw_events(cbus[0]) == ["1 staged; the MCU installs it after power-off"]
    assert sim.mcu.fw_staged and frames(sim, "mcu-rx")[-1].payload == bytes([P.BYE_NO_HA])
    assert sim.mcu.history[-1][0] < 0.3 + 25.0 + 0.5 + 6.0


def test_firmware_in_bench_mode_is_pushed_with_the_radio_on(host_exe):
    sim, cbus, res = run(host_exe, Ha(fwu=FWU), bench=True, limit=30.0)
    c = cbus[0]
    assert c.responses[999] == (True, "")             # answered after FW_END
    assert c.shutdowns == 0 and fw_events(c) == ["1 staged; the MCU installs it after power-off"]
    assert P.FW_END in [f.cmd for f in frames(sim, "mcu-rx")]


def test_firmware_heap_check(host_exe):
    sim, cbus, res = run(host_exe, Ha(fwu=FWU), heap=50000)
    c = cbus[0]
    assert c.responses[2000] == (False, "not enough RAM: the package needs 40032 B + 20480 B reserve, "
                                        "50000 B free")
    assert not any(f.cmd == P.FW_BEGIN for f in frames(sim, "mcu-rx"))


# th5_firmware calls straight into the engine, after a HELLO_RSP

class Linked:
    def __init__(self, exe, boot_ver=1, **opts):
        self.e = Engine(exe, **opts)
        ev = self.e.cmd("start", 0)
        hello = P.decode_frame(bytes.fromhex(next(x for x in ev if x.startswith("tx "))[3:]))
        rsp = P.HelloRsp(baud=P.BAUD_SLOW, rx_max=1024, boot_ver=boot_ver, fw_version=0x00020000)
        ev = self.e.cmd("feed", 10, P.encode_frame(hello.seq, P.HELLO_RSP, rsp.encode()).hex())
        assert any(x.startswith("hello ") for x in ev)
        self.call = 0

    def send(self, kind, off, data, ms=100):
        self.call += 1
        ev = self.e.cmd(kind, ms, self.call, off, base64.b64encode(data).decode())
        r = [x for x in ev if x.startswith(f"respond {self.call} ")]
        assert len(r) == 1, ev
        ok, _, msg = r[0].split(" ", 2)[2].partition(" ")
        return ok == "1", msg

    def upload(self, pkg, chunk=4096):
        out = []
        for off in range(0, len(pkg), chunk):
            out.append(self.send("firmware", off, pkg[off:off + chunk]))
        return out


def pkg_with(**hdr):
    b = bytearray(FWU)
    for off, val in hdr.values():
        b[off:off + 4] = val.to_bytes(4, "little")
    return bytes(b)


@pytest.mark.parametrize("pkg,msg", [
    (b"XXXX" + FWU[4:], "not a TH5U firmware package"),
    (pkg_with(load=(16, 0x2000)), "bad package header: load 0x00002000 (0x00001000)"),
    (pkg_with(length=(8, 40002)), "bad package header: length 40002 (64..50144, a multiple of 4)"),
    (pkg_with(length=(8, 50148)), "bad package header: length 50148 (64..50144, a multiple of 4)"),
    (pkg_with(length=(8, 60)), "bad package header: length 60 (64..50144, a multiple of 4)"),
    (pkg_with(min_boot=(20, 2)), "the package needs bootloader v2, the MCU has v1"),
    (FWU[:20], "the first chunk must hold the 32-byte package header"),
    (P.fwu_package(IMAGE[:64], 1) + b"\0" * 4, "100 bytes, but the header says 96"),
])
def test_firmware_header_refused_at_once(host_exe, pkg, msg):
    lk = Linked(host_exe)
    try:
        assert lk.send("firmware", 0, pkg[:4096]) == (False, msg)
    finally:
        lk.e.close()


def test_firmware_crc_mismatch_fails_the_last_call(host_exe):
    bad = bytearray(FWU)
    bad[-1] ^= 1
    lk = Linked(host_exe)
    try:
        r = lk.upload(bytes(bad))
        assert all(ok for ok, _ in r[:-1])
        crc_bad = P.crc32(bytes(bad[32:]))
        assert r[-1] == (False, f"image CRC {crc_bad:08x} != header {P.crc32(IMAGE):08x}")
        # the buffer is gone: only offset 0 is accepted now
        assert lk.send("firmware", 4096, bad[4096:8192]) == (False, "offset 4096: expected 0..0, total <= 50176")
        assert all(ok for ok, _ in lk.upload(FWU))
    finally:
        lk.e.close()


def test_firmware_buffering_rules(host_exe):
    lk = Linked(host_exe)
    try:
        assert lk.send("firmware", 0, FWU[:4096]) == (True, "")
        assert lk.send("firmware", 8192, FWU[8192:12288]) == (False, "offset 8192: expected 0..4096, total <= 40032")
        assert lk.send("firmware", 4096, FWU[4096:] + b"\0" * 4) == (False, "offset 4096: expected 0..4096, "
                                                                        "total <= 40032")
        assert lk.send("firmware", 2048, FWU[2048:8192]) == (True, "")         # overlap is fine
        r = [lk.send("firmware", off, FWU[off:off + 4096]) for off in range(8192, len(FWU), 4096)]
        assert all(ok for ok, _ in r)
        # complete: frozen, except byte-identical retries; no frame in this session
        assert lk.send("firmware", 4096, FWU[4096:8192]) == (True, "")
        assert lk.send("firmware", 4096, bytes(4096)) == (False, "firmware already complete: restart the "
                                                                 "upload at offset 0")
        assert lk.send("frame", 0, SLOT[:3072]) == (False, "an MCU firmware package is buffered: no frame "
                                                           "in this session")
        # a new upload at offset 0 replaces it
        assert lk.send("firmware", 0, FWU[:4096]) == (True, "")
    finally:
        lk.e.close()


def test_firmware_upload_drops_the_frame_and_frame_drops_a_partial_package(host_exe):
    lk = Linked(host_exe)
    try:
        for off in range(0, len(SLOT), 3072):
            assert lk.send("frame", off, SLOT[off:off + 3072]) == (True, "")
        assert lk.send("firmware", 0, FWU[:4096]) == (True, "")             # frame buffer released
        assert lk.send("frame", 3072, SLOT[3072:6144]) == (False, "offset 3072: expected 0..0, total <= 25088")
        assert lk.send("frame", 0, SLOT[:3072]) == (True, "")               # partial package dropped
        assert lk.send("firmware", 4096, FWU[4096:8192]) == (False, "offset 4096: expected 0..0, "
                                                                    "total <= 50176")
    finally:
        lk.e.close()


def test_firmware_refused_without_hello_boot_ver(host_exe):
    lk = Linked(host_exe, boot_ver=0)
    try:
        assert lk.send("firmware", 0, FWU[:4096]) == (False, "the MCU has no bootloader (boot_ver 0): "
                                                             "install it over SWD")
    finally:
        lk.e.close()


@pytest.mark.parametrize("tail,ack,expected", [
    (b"", True, b"\0"),  # old MCU, no implicit delivery receipt
    (b"\0\0", False, b"\0"),  # old HA keeps records
    (b"\0\0", True, b"\0\1"),
    (b"\1\0" + b"x" * 17, True, b"\0\1"),
    (b"\10\0" + b"x" * 136, True, b"\0\1"),
    (b"\11\0" + b"x" * 153, True, b"\0"),  # >8
    (b"\1\1" + b"x" * 17, True, b"\0"),  # unknown flags
    (b"\1\0" + b"x" * 16, True, b"\0"),  # truncated record
    (b"\1\0" + b"x" * 18, True, b"\0\1"),  # trailing bytes from a newer MCU: ignored
])
def test_history_tail_and_explicit_delivery_ack(host_exe, tail, ack, expected):
    class TailMcu(McuModel):
        def _hello(self, f, now):
            cmd, payload, after = super()._hello(f, now)
            return cmd, payload[:44] + tail, after

        def _bye(self, p, now):
            return super()._bye(p[:1], now)

    sim, cbus, res = run(host_exe, Ha(history_ack=ack), mcu=TailMcu())
    bye = [f for f in frames(sim, "mcu-rx") if f.cmd == P.BYE][-1]
    assert bye.payload == expected
    histories = [e.removeprefix("history ") for _, e in cbus[0].events if e.startswith("history ")]
    records = tail[:2 + 17 * tail[0]] if tail else b""
    assert histories == [records.hex() if len(expected) == 2 or tail == b"\0\0" else ""]


def test_wifi_up_reported_before_api_timeout(host_exe):
    sim, _, _ = run(host_exe, Ha(api_s=None))
    states = [f.payload[0] for f in frames(sim, "mcu-rx") if f.cmd == P.NET]
    assert states == [P.NET_WIFI_CONNECTING, P.NET_WIFI_UP, P.NET_FAIL_API]
