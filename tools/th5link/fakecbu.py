"""Scripted CBU side of the link (docs/protocol.md section 7).

    HELLO -> [baud probe PING] -> NET -> TIME -> CONFIG -> [HOLD]
          -> [IMG_BEGIN / IMG_DATA... / IMG_END | FW_BEGIN / IMG_DATA... / FW_END] -> BYE

It plays what th5_link will do on the BK7231N, minus Wi-Fi and HA. The script
is a generator driven by the same Endpoint engine as the MCU model, so it runs
in virtual time (sim.py, the tests) or against real hardware:

    uv run --with pyserial python -m tools.th5link.fakecbu uart /dev/cu.usbserial-X --slot ha/out/frame-C.slot
    uv run --with pyocd   python -m tools.th5link.fakecbu swd --elf firmware/build/nodeep/th5.elf --slot ha/out/frame-C.slot

(SWD mailbox run on the TH5 bench: firmware/notes-link.md. UART not yet.)
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from custom_components.th5_display.timeinfo import time_fields

from . import proto as P
from .link import Endpoint, LinkFailed

BOOT_TEXT = (b"\r\nV:BK7231N_1.0.1\r\nREG:cpsr spsr r13 r14\r\nSVC:00401d8c 004011f3\r\n"
             b"bk_misc_init_start_type 0 0\r\nlt_main(): LibreTiny v1.8\r\n")


MAX_RESYNCS = 16                       # E_OFFSET resyncs per frame before giving up


def local_zone() -> dt.tzinfo:
    """The host's zone: the ZoneInfo /etc/localtime points at, else its current
    fixed offset (no DST data then)."""
    try:
        return ZoneInfo(str(Path("/etc/localtime").resolve()).split("zoneinfo/", 1)[1])
    except (OSError, IndexError, ValueError, KeyError):
        return dt.timezone(dt.timedelta(seconds=time.localtime().tm_gmtoff))


@dataclass
class CbuScript:
    """What the fake CBU does in one session."""
    fw_version: int = 0x00010000
    boot_delay: float = 0.300          # BK boot to first HELLO
    boot_text: bytes = BOOT_TEXT
    wifi_s: float = 1.5                # Wi-Fi + API connect time
    net_fail: int | None = None        # e.g. P.NET_FAIL_WIFI
    ha_present: bool = True
    utc0: int = 1793520000             # epoch at sim time 0 (2026-11-01T08:00Z)
    utc_fn: object = None              # callable(now) -> utc; default utc0 + now
    tz: dt.tzinfo = ZoneInfo("Europe/Oslo")   # TIME offset and next DST change come from this
    config: dict = field(default_factory=lambda: {P.CFG_MODE: P.MODE_FORECAST, P.CFG_SYNC_MIN: 60,
                                                  P.CFG_CLOCK_MIN: 1, P.CFG_CLOCK_24H: 1,
                                                  P.CFG_FULL_N: 30, P.CFG_T_OFF: 0,
                                                  P.CFG_RH_OFF: 0, P.CFG_FLAGS: 0})
    hold_s: int = 0
    hold_dwell: float = 0.0            # stay this long after HOLD (OTA, adoption)
    slot: bytes | None = None          # a frame slot image (ha/out/*.slot)
    chunk: int = P.IMG_CHUNK
    hello_wait: float = 0.0            # keep re-sending HELLO this long (UART bench)
    always_begin: bool = False         # send IMG_BEGIN even if HELLO_RSP has the id (tests HAVE)
    slow_only: bool = False            # HELLO flag: stay at 115200 (th5_link fast_baud: false)
    fwu: bytes | None = None           # an MCU firmware package (.fwu): sent instead of the frame


class FakeCbu:
    def __init__(self, script: CbuScript | None = None, sim_boot: bool = True):
        self.s = script or CbuScript()
        self.ep = Endpoint("cbu", self._handle, P.MAX_TO_CBU, P.MAX_TO_MCU)
        self.sim_boot = sim_boot
        self.gen = None
        self.wake_at: float | None = None
        self.now = 0.0
        self.done = False
        self.error: str | None = None
        self.hello_rsp: P.HelloRsp | None = None
        self.forgotten = False
        self.img_result: str | None = None
        self.fw_result: str | None = None
        self.log: list[str] = []

    # --- runner interface ----------------------------------------------------
    @property
    def baud(self) -> int:
        return self.ep.baud

    def start(self, now: float):
        self.now = now
        if self.sim_boot and self.s.boot_text:
            self.ep.tx += self.s.boot_text
        self.gen = self._script()
        self._resume(None, now)

    def feed(self, data: bytes, now: float):
        self.now = now
        self.ep.feed(data, now)

    def poll(self, now: float):
        self.now = now
        self.ep.tick(now)
        if self.wake_at is not None and now >= self.wake_at:
            self.wake_at = None
            self._resume(None, now)

    def take_tx(self) -> bytes:
        return self.ep.take_tx()

    def tx_done(self, now: float):
        self.ep.tx_done(now)

    def next_deadline(self) -> float | None:
        ts = [t for t in (self.wake_at, self.ep.next_deadline()) if t is not None]
        return min(ts) if ts else None

    # --- coroutine plumbing ----------------------------------------------------
    def _resume(self, value, now, exc=None):
        self.now = now
        if self.done:
            return
        try:
            op = self.gen.throw(exc) if exc else self.gen.send(value)
        except StopIteration:
            self.done = True
            return
        except LinkFailed as e:
            self.done, self.error = True, f"link failed: {e}"
            return
        kind = op[0]
        if kind == "req":
            _, cmd, payload, probe = op

            def done(f, t, cmd=cmd):
                if f is None:
                    self._resume(None, t, LinkFailed(P.CMD_NAMES[cmd]))
                else:
                    self._resume(f, t)
            self.ep.request(cmd, payload, now, on_done=done, probe=probe)
        elif kind == "sleep":
            self.wake_at = now + op[1]
        elif kind == "baud":
            self.ep.set_baud(op[1], now)
            self._resume(None, now)

    @staticmethod
    def req(cmd, payload=b"", probe=False):
        return ("req", cmd, payload, probe)

    def _log(self, msg):
        self.log.append(f"{self.now:8.3f} {msg}")

    # --- MCU -> CBU requests -----------------------------------------------------
    def _handle(self, f: P.Frame, now: float):
        if f.cmd == P.FORGET:
            self.forgotten = True             # th5_link: clear saved Wi-Fi/API prefs, sync flash
            return (P.ACK, P.ack_encode(P.FORGET))
        if f.cmd == P.PING:
            return (P.ACK, P.ack_encode(P.PING, f.payload[:P.PING_MAX]))
        return (P.ERR, P.Err(f.cmd, P.E_UNKNOWN_CMD).encode())

    # --- the session -------------------------------------------------------------
    def utc(self) -> int:
        return int(self.s.utc_fn(self.now) if self.s.utc_fn else self.s.utc0 + self.now)

    def _script(self):
        s = self.s
        if self.sim_boot:
            yield ("sleep", s.boot_delay)
        give_up = self.now + s.hello_wait
        while True:
            try:
                rsp = yield self.req(P.HELLO, P.Hello(P.VER, P.MAX_TO_CBU, s.fw_version,
                                                    P.HELLO_F_SLOW_ONLY if s.slow_only else 0).encode())
                break
            except LinkFailed:
                if self.now >= give_up:
                    raise
        if rsp.cmd != P.HELLO_RSP:
            self._log(f"HELLO refused: {P.describe(rsp)}")
            yield self.req(P.BYE, P.bye_encode(P.BYE_LINK))
            return
        hr = self.hello_rsp = P.HelloRsp.parse(rsp.payload)
        self._log(f"HELLO_RSP {hr}")
        if hr.baud == P.BAUD_FAST:
            yield ("baud", P.BAUD_FAST)
            yield ("sleep", P.T_PROBE_GAP)
            yield self.req(P.PING, bytes([0x55, 0xAA, 0x00, 0xFF]), probe=True)
        if hr.flags & P.F_FORGET_PENDING:
            for _ in range(50):               # the MCU sends FORGET right after the probe
                if self.forgotten:
                    break
                yield ("sleep", 0.02)
            yield self.req(P.BYE, P.bye_encode(P.BYE_FORGOT))
            return
        yield self.req(P.NET, P.Net(P.NET_WIFI_CONNECTING).encode())
        yield ("sleep", s.wifi_s)
        if s.net_fail is not None:
            yield self.req(P.NET, P.Net(s.net_fail).encode())
            yield self.req(P.BYE, P.bye_encode(P.BYE_NET_FAIL))
            return
        yield self.req(P.NET, P.Net(P.NET_API_UP, 0, -58).encode())
        if not s.ha_present:
            yield ("sleep", 3.0)              # th5_link: no th5_config within 3 s
            yield self.req(P.BYE, P.bye_encode(P.BYE_NO_HA))
            return
        utc = self.utc()
        t = P.Time(utc, *time_fields(s.tz, utc))
        self._check((yield self.req(P.TIME, t.encode())))
        self._check((yield self.req(P.CONFIG, P.config_encode(s.config))))
        yield self.req(P.NET, P.Net(P.NET_HA_CONFIG, 0, -58).encode())
        if s.hold_s:
            self._check((yield self.req(P.HOLD, P.hold_encode(s.hold_s))))
            yield ("sleep", s.hold_dwell)
        if s.fwu is not None:
            yield from self._send_firmware()       # protocol.md 6 item 6.4: no frame then
        elif s.slot is not None:
            yield from self._send_frame(hr)
        yield self.req(P.BYE, P.bye_encode(P.BYE_DONE))

    def _check(self, rsp):
        if rsp.cmd == P.ERR:
            self._log(f"refused: {P.describe(rsp)}")
            return False
        return True

    def _send_frame(self, hr: P.HelloRsp):
        b, payload = P.ImgBegin.from_slot(self.s.slot)
        if b.frame_id == hr.frame_id and not self.s.always_begin:
            self.img_result = "same id"      # HA would not even push it
            return
        rsp = yield self.req(P.IMG_BEGIN, b.encode())
        if not self._check(rsp):
            self.img_result = "refused"
            return
        if P.ack_parse(rsp.payload)[1][:1] == bytes([P.IMG_HAVE]):
            self.img_result = "have"
            return
        err = yield from self._send_data(payload)
        if err:
            self.img_result = err
            return
        rsp = yield self.req(P.IMG_END, P.img_end_encode(b.frame_id))
        self.img_result = "committed" if self._check(rsp) else "crc refused"

    def _send_firmware(self):
        b, image, _min_boot = P.fwu_parse(self.s.fwu)
        rsp = yield self.req(P.FW_BEGIN, b.encode())
        if not self._check(rsp):
            self.fw_result = f"refused {P.Err.parse(rsp.payload)}"
            return
        if P.ack_parse(rsp.payload)[1][:1] == bytes([P.IMG_HAVE]):
            self.fw_result = "have"
            return
        err = yield from self._send_data(image)
        if err:
            self.fw_result = err
            return
        rsp = yield self.req(P.FW_END, P.fw_end_encode(b.crc32))
        self.fw_result = "staged" if self._check(rsp) else f"refused {P.Err.parse(rsp.payload)}"

    def _send_data(self, payload: bytes):
        """IMG_DATA x n (frame payload or firmware image offsets). None, or an error."""
        off = 0
        resyncs = 0
        while off < len(payload):
            data = payload[off:off + self.s.chunk]
            rsp = yield self.req(P.IMG_DATA, P.img_data_encode(off, data))
            if rsp.cmd == P.ERR:
                e = P.Err.parse(rsp.payload)
                if e.code == P.E_OFFSET and e.detail <= len(payload) and resyncs < MAX_RESYNCS:
                    resyncs += 1
                    off = e.detail             # resync to the MCU's next offset
                    continue
                return f"error {e}"
            off = P.u32(P.ack_parse(rsp.payload)[1], 0)
        return None


def main(argv=None):
    from . import transport as T
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="kind", required=True)
    u = sub.add_parser("uart")
    u.add_argument("port")
    w = sub.add_parser("swd")
    w.add_argument("--elf", required=True)
    w.add_argument("--pack", default=str(Path.home() / ".cache/hc32/HC32L17X.pack"))
    for p in (u, w):
        p.add_argument("--slot", type=Path, help="frame slot image to push (ha/out/*.slot)")
        p.add_argument("--fwu", type=Path, help="MCU firmware package to push instead (th5.fwu)")
        p.add_argument("--hold", type=int, default=0)
        p.add_argument("--mode", choices=["picture", "forecast"], default="forecast")
        p.add_argument("--sync-min", type=int, default=60)
        p.add_argument("--timeout", type=float, default=60.0)
        p.add_argument("--force-begin", action="store_true",
                       help="send IMG_BEGIN even if the MCU reports the same frame id (tests HAVE)")
        p.add_argument("--tz", help="IANA zone for the TIME offset and DST data (default: the host's)")
        p.add_argument("--t-off", type=int, default=0, help="CONFIG T_OFF, 0.01 degC")
        p.add_argument("--slow-only", action="store_true",
                       help="set the HELLO flag that keeps the link at 115200")
    w.add_argument("--trigger", type=int, default=None,
                   help="bench builds: start the session from SWD with this trigger (5 = BENCH)")
    a = ap.parse_args(argv)
    script = CbuScript(wifi_s=0.2, utc_fn=lambda now: time.time(), hold_s=a.hold,
                       hello_wait=30.0 if a.kind == "uart" else 0.0,
                       slot=a.slot.read_bytes() if a.slot else None, always_begin=a.force_begin,
                       fwu=a.fwu.read_bytes() if a.fwu else None,
                       slow_only=a.slow_only)
    script.config[P.CFG_MODE] = P.MODE_PICTURE if a.mode == "picture" else P.MODE_FORECAST
    script.config[P.CFG_SYNC_MIN] = a.sync_min
    script.config[P.CFG_T_OFF] = a.t_off
    script.tz = ZoneInfo(a.tz) if a.tz else local_zone()
    tr = T.SerialTransport(a.port) if a.kind == "uart" else T.SwdMailboxTransport(a.elf, a.pack)
    if a.kind == "swd" and a.trigger is not None:
        tr.request_session(a.elf, a.trigger)
    cbu = FakeCbu(script, sim_boot=False)
    cbu.ep.trace = lambda d, f, t: print(f"{t:8.3f} {d} {P.describe(f)}")
    try:
        T.run_realtime(cbu, tr, timeout=a.timeout)
    finally:
        tr.close()
    print("\n".join(cbu.log))
    print("result:", cbu.error or "ok", "| frame:", cbu.img_result, "| firmware:", cbu.fw_result,
          f"| SWD WAIT retries: {getattr(tr, 'wait_retries', 0)}")
    return 0 if not cbu.error else 1


if __name__ == "__main__":
    sys.exit(main())
