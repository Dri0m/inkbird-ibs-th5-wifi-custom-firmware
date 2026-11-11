"""Executable model of the MCU side: session state machine, request handling,
frame slots and settings (docs/protocol.md sections 5 and 6).

It is the reference the C firmware is checked against, and it doubles as a fake
MCU for the M3a ESPHome spike (run it over a serial port with
transport.run_realtime).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import proto as P
from .link import Endpoint

LINK_WAIT, LINK_OK, LINK_ERR = 0, 1, 2       # firmware/src/render.h
STATUS_NONE, STATUS_NOT_SET_UP, STATUS_LINK_ERROR = 0, 1, 2
# as link.h: why a session after HELLO failed (fallback screen message)
STATUS_WIFI, STATUS_HA_UNREACH, STATUS_HA_NO_ANSWER = 5, 6, 7

BATT_SESSION_MV = 3650
NOHELLO_RETRY_S = (60, 300)                  # then 24 h
NOHELLO_BACKOFF_S = 24 * 3600
BACKOFF_CAP_S = 6 * 3600


@dataclass
class Slot:
    seq: int
    frame_id: int
    bpp: int
    encoding: int
    flags: int
    length: int
    crc32: int
    data: bytearray
    committed: bool = False


@dataclass
class ImgXfer:
    begin: P.ImgBegin | P.FwBegin
    slot: int
    buf: bytearray                     # where IMG_DATA lands: a slot's data, or the staged image
    next: int = 0
    fw: bool = False                   # a firmware transfer (FW_BEGIN, 4.14)


@dataclass
class Reading:
    t_centi: int = 2140
    rh_centi: int = 5280
    batt_mv: int = 3980
    batt_pct: int = 78
    charge: int = P.CHG_DISCHARGING
    sensor_ok: bool = True


def next_after(result: int, sync_min: int, fail_count: int, nohello_count: int) -> int:
    """Seconds until the next scheduled session after `result` (protocol.md 5.4).
    fail_count / nohello_count are the counts *after* this result."""
    interval = sync_min * 60
    if result == P.RES_OK:
        return interval
    if result in (P.RES_NO_HELLO, P.RES_TUYA):
        if nohello_count <= len(NOHELLO_RETRY_S):
            return NOHELLO_RETRY_S[nohello_count - 1]
        return NOHELLO_BACKOFF_S
    if result in (P.RES_NET_FAIL, P.RES_CAP, P.RES_LINK):
        return min(interval * 2 ** fail_count, BACKOFF_CAP_S)
    return interval                          # RES_ABORT: keep the schedule


class McuModel:
    FW_VERSION = 0x00010000
    BOOT_VER = 1                       # the bootloader (design 4.7); 0 = none

    def __init__(self, reset_cause: int = P.RST_POR):
        self.ep = Endpoint("mcu", self._handle, P.MAX_TO_MCU, P.MAX_TO_CBU)
        self.reset_cause = reset_cause
        self.state = "OFF"
        self.cbu_on = False
        self.slots: list[Slot | None] = [None, None]
        self.settings = P.Settings()
        self.settings_writes = 0
        self.history_hours: list[P.HistoryHour] = []
        self.history_sent: list[P.HistoryHour] = []
        self.time: P.Time | None = None
        self.time_set_at: float | None = None
        self.units = 0
        self.reading = Reading()
        self.trigger = P.TRIG_BOOT
        self.bench = False
        self.forget_pending = False
        self.factory_reset = False
        self.forget_sent = False
        self.img: ImgXfer | None = None
        # MCU firmware (design 4.7): the running app, and what FW_END staged
        self.fw_version = self.FW_VERSION
        self.boot_ver = self.BOOT_VER
        self.app = b""                     # installed image at 0x1000 (for HAVE)
        self.staged: tuple[int, bytes] | None = None
        self.fw_done = False               # FW_END succeeded this session
        self.fw_staged = False             # ... in the last session (the outcome)
        self.installs = 0
        self.net: P.Net | None = None
        self.fail_count = 0
        self.nohello_count = 0
        self.last_result = P.RES_NONE
        self.last_session_ms = 0
        self.result: int | None = None
        self.link_icon = LINK_WAIT
        self.status = STATUS_NOT_SET_UP
        self.next_session_in: int | None = None
        self.refresh: str | None = None
        self.committed_this_session = False
        self.t_on = 0.0
        self.deadline = 0.0
        self.hello_deadline = 0.0
        self.probe_deadline = 0.0
        self.probe_start = 0.0
        self.off_at: float | None = None
        self.closing_at = 0.0
        self.history: list[tuple] = []

    # --- storage ---------------------------------------------------------
    def active_slot(self) -> Slot | None:
        ok = [s for s in self.slots if s and s.committed]
        return max(ok, key=lambda s: s.seq) if ok else None

    @property
    def frame_id(self) -> int:
        s = self.active_slot()
        return s.frame_id if s else 0

    def load_slot_image(self, slot_bytes: bytes, index: int = 0):
        b, payload = P.ImgBegin.from_slot(slot_bytes)
        self.slots[index] = Slot(1, b.frame_id, b.bpp, b.encoding, b.flags, b.length, b.crc32,
                                 bytearray(payload), True)

    # --- session ---------------------------------------------------------
    def start_session(self, now: float, trigger: int = P.TRIG_TIMER, bench: bool = False) -> bool:
        if self.cbu_on:
            return True
        if self.reading.batt_mv < BATT_SESSION_MV and self.reading.charge == P.CHG_DISCHARGING:
            return False                      # skipped, not a failure (design 4.6)
        self.trigger = P.TRIG_BENCH if bench else trigger
        self.bench = bench
        self.cbu_on = True
        self.state = "WAIT_HELLO"
        self.t_on = now
        self.hello_deadline = now + P.T_HELLO
        self.deadline = now + (P.T_HOLD_MAX if bench else P.T_CAP)
        self.holding = bench
        self.result = None
        self.off_at = None
        self.closing_at = 0.0
        self.hold_seen = self.got_hello = False
        self.img = None
        self.net = None
        self.forget_sent = False
        self.committed_this_session = False
        self.fw_done = self.fw_staged = False
        self.ep.session_reset()
        return True

    def end_session(self, now: float, result: int):
        self.cbu_on = False
        self.state = "OFF"
        self.img = None
        self.ep.session_reset()
        self.last_session_ms = int(round((now - self.t_on) * 1000))
        self.last_result = result
        icon = self.link_icon
        # A maintenance (HOLD) or bench session that ends at the hold cap is not a failure.
        maint = result == P.RES_CAP and (getattr(self, "hold_seen", False)
                                         or (self.bench and getattr(self, "got_hello", False)))
        if maint:
            pass                                  # icon and counters as for ABORT
        elif result == P.RES_OK:
            self.fail_count = self.nohello_count = 0
            self.link_icon, self.status = LINK_OK, STATUS_NONE
        elif result in (P.RES_NO_HELLO, P.RES_TUYA):
            self.nohello_count += 1
            self.fail_count += 1
            self.link_icon = LINK_ERR
            self.status = STATUS_LINK_ERROR
        elif result != P.RES_ABORT:
            self.fail_count += 1
            self.link_icon = LINK_ERR
            self.status = self._net_status(result)
        self.next_session_in = next_after(P.RES_OK if maint else result,
                                          self.settings.values[P.CFG_SYNC_MIN],
                                          self.fail_count, self.nohello_count)
        # Panel work only after the CBU is off (design 3 step 10).
        self.refresh = "full" if self.committed_this_session else (
            "band" if icon != self.link_icon else None)
        self.history.append((now, result))
        self.result = result
        self.fw_staged = self.fw_done
        if self.fw_done:
            self.install()

    def install(self):
        """The reset after a staged FW_END: the bootloader copies the image and
        the new app boots with no frame (design 4.7)."""
        ver, image = self.staged
        self.fw_version, self.app = ver, image
        self.staged = None
        self.slots = [None, None]
        self.installs += 1

    def switch_off(self, now: float):
        """PA09 falling: abort any session."""
        if self.cbu_on:
            self.end_session(now, P.RES_ABORT)

    # --- runner interface --------------------------------------------------
    @property
    def baud(self) -> int:
        return self.ep.baud

    def feed(self, data: bytes, now: float):
        if not self.cbu_on:
            return
        self.ep.feed(data, now)
        if self.state == "WAIT_HELLO" and self.ep.parser.tuya and self.cbu_on:
            self.end_session(now, P.RES_TUYA)
            return
        if (self.state == "PROBE" and self.ep.last_valid_rx is not None
                and self.ep.last_valid_rx > self.probe_start):
            self.state = "LINKED"             # a valid frame at 500 k: keep it

    def take_tx(self) -> bytes:
        return self.ep.take_tx() if self.cbu_on else b""

    def tx_done(self, now: float):
        self.ep.tx_done(now)

    def next_deadline(self) -> float | None:
        if not self.cbu_on:
            return None
        ts = [self.deadline]
        if self.state == "WAIT_HELLO" and not self.holding:
            ts.append(self.hello_deadline)
        if self.state == "PROBE":
            ts.append(self.probe_deadline)
        if self.off_at is not None:
            ts.append(self.off_at)
        if self.state == "CLOSING":
            ts.append(self.closing_at + P.T_BYE_TC_MAX)
        d = self.ep.next_deadline()
        if d is not None:
            ts.append(d)
        return min(ts)

    def poll(self, now: float):
        if not self.cbu_on:
            return
        self.ep.tick(now)
        if self.off_at is not None and now >= self.off_at:
            self.end_session(now, self.result_pending)
            return
        if self.state == "CLOSING":
            # off_at (TC + 50 ms) normally ends it; bounded in case TC never comes
            if now >= self.closing_at + P.T_BYE_TC_MAX or now >= self.deadline:
                self.end_session(now, self.result_pending)
            return
        if now >= self.deadline:
            self.end_session(now, P.RES_NET_FAIL if self.state == "GRACE" else P.RES_CAP)
            return
        if self.state == "WAIT_HELLO" and not self.holding and now >= self.hello_deadline:
            self.end_session(now, P.RES_NO_HELLO)
            return
        if self.state == "PROBE" and now >= self.probe_deadline:
            self.ep.revert(now)
            self.state = "LINKED"
        if (self.state == "LINKED" and self.forget_pending and not self.forget_sent
                and self.ep.out is None):
            self.forget_sent = True
            self.ep.request(P.FORGET, b"", now, on_done=self._forget_done)

    def _forget_done(self, f, now):
        if f is not None and f.cmd == P.ACK:
            self.forget_pending = False

    # --- request handling ---------------------------------------------------
    def _err(self, cmd, code, detail=0):
        return (P.ERR, P.Err(cmd, code, detail).encode())

    def _ack(self, cmd, data=b""):
        return (P.ACK, P.ack_encode(cmd, data))

    def _handle(self, f: P.Frame, now: float):
        cmd = f.cmd
        if cmd not in P.CBU_REQUESTS:
            return self._err(cmd, P.E_UNKNOWN_CMD)
        if len(f.payload) < P.MIN_LEN[cmd]:
            return self._err(cmd, P.E_BAD_LEN, P.MIN_LEN[cmd])
        if cmd == P.HELLO and self.state != "CLOSING":   # after BYE: refused (5.3)
            return self._hello(f, now)
        if self.state in ("WAIT_HELLO", "CLOSING"):
            return self._err(cmd, P.E_STATE)
        return getattr(self, "_" + P.CMD_NAMES[cmd].lower())(f.payload, now)

    def hello_rsp(self, baud: int) -> P.HelloRsp:
        r = self.reading
        flags = ((P.F_TIME_VALID if self.time else 0) | (P.F_FRAME_VALID if self.active_slot() else 0)
                 | (P.F_SENSOR_OK if r.sensor_ok else 0)
                 | (P.F_FORGET_PENDING if self.forget_pending else 0)
                 | (P.F_BENCH if self.bench else 0) | (P.F_FACTORY_RESET if self.factory_reset else 0))
        return P.HelloRsp(P.VER, self.reset_cause, baud, P.MAX_TO_MCU, self.fw_version, self.frame_id,
                          self.settings.crc, self.units, self.trigger, flags,
                          r.t_centi if r.sensor_ok else P.T_NONE,
                          r.rh_centi if r.sensor_ok else P.RH_NONE,
                          r.batt_mv, r.batt_pct, r.charge, self.last_session_ms,
                          self.last_result, min(self.fail_count, 255), self.boot_ver)

    def _hello(self, f, now):
        h = P.Hello.parse(f.payload)
        if h.proto_max < 1:
            return self._err(P.HELLO, P.E_VERSION, P.VER)
        self.img = None                       # a (re)booted CBU: no resume
        self.got_hello = True
        slow = self.ep.sticky_slow or h.flags & P.HELLO_F_SLOW_ONLY
        baud = P.BAUD_SLOW if slow else P.BAUD_FAST
        self.state = "HELLO_SENT"
        self.history_sent = self.history_hours[:max(0, min(8, (h.rx_max - 46) // 17))]
        rsp = self.hello_rsp(baud)
        rsp.history = self.history_sent if h.rx_max >= 46 else None
        payload = rsp.encode()

        def after(t):
            if baud == P.BAUD_FAST:
                self.ep.set_baud(P.BAUD_FAST, t)
                self.state = "PROBE"
                self.probe_start = t
                self.probe_deadline = t + P.T_PROBE
            else:
                self.state = "LINKED"
        return (P.HELLO_RSP, payload, after)

    def _ping(self, p, now):
        if len(p) > P.PING_MAX:
            return self._err(P.PING, P.E_BAD_LEN, P.PING_MAX)
        return self._ack(P.PING, p)

    def _net_status(self, result):
        """link.c net_status(): how far the CBU got, for the fallback message."""
        if result == P.RES_LINK:
            return STATUS_LINK_ERROR
        st = self.net.state if self.net else 0
        if st in (0, P.NET_WIFI_CONNECTING, P.NET_FAIL_WIFI, P.NET_FAIL_NO_CREDENTIALS):
            return STATUS_WIFI
        if st in (P.NET_WIFI_UP, P.NET_FAIL_API):
            return STATUS_HA_UNREACH
        return STATUS_HA_NO_ANSWER

    def _net(self, p, now):
        self.net = P.Net.parse(p)
        if self.net.failed and self.state != "GRACE":
            self.state = "GRACE"
            self.result_pending = P.RES_NET_FAIL
            self.deadline = min(self.deadline, now + P.T_NET_GRACE)
        return self._ack(P.NET)

    def _time(self, p, now):
        t = P.Time.parse(p)
        bad = t.check()
        if bad is not None:
            return self._err(P.TIME, P.E_BAD_VALUE, bad)
        self.time, self.time_set_at = t, now
        return self._ack(P.TIME)

    def _config(self, p, now):
        vals, bad = P.config_parse(p)
        if bad is not None:
            return self._err(P.CONFIG, P.E_BAD_LEN, bad)
        tag = P.config_check(vals)
        if tag is not None:
            return self._err(P.CONFIG, P.E_BAD_VALUE, tag)
        new = {**self.settings.values, **vals}
        if P.settings_crc(new) != self.settings.crc:
            self.settings.values = new
            self.settings_writes += 1         # ping-pong sector write, before the ACK
        return self._ack(P.CONFIG)

    def _img_begin(self, p, now):
        if self.fw_done:
            return self._err(P.IMG_BEGIN, P.E_STATE, P.FWS_STAGED)
        b = P.ImgBegin.parse(p)
        if b.frame_id == 0:
            return self._err(P.IMG_BEGIN, P.E_BAD_VALUE, 0)
        if b.bpp not in (1, 2):
            return self._err(P.IMG_BEGIN, P.E_BAD_VALUE, 8)
        if b.encoding != 0:
            return self._err(P.IMG_BEGIN, P.E_BAD_VALUE, 9)
        if b.length > P.IMG_MAX or b.length != b.bpp * P.FRAME_PLANE_BYTES:
            return self._err(P.IMG_BEGIN, P.E_TOO_BIG, P.IMG_MAX)
        act = self.active_slot()
        if act and act.frame_id == b.frame_id:
            self.img = None
            return self._ack(P.IMG_BEGIN, bytes([P.IMG_HAVE]))
        idx = 0 if act is None else 1 - self.slots.index(act)
        seq = (act.seq + 1) if act else 1
        # Header programmed now, commit word left erased; sectors erased lazily.
        self.slots[idx] = Slot(seq, b.frame_id, b.bpp, b.encoding, b.flags, b.length, b.crc32,
                               bytearray(b"\xff" * b.length), False)
        self.img = ImgXfer(b, idx, self.slots[idx].data)
        return self._ack(P.IMG_BEGIN, bytes([P.IMG_START]))

    def _img_data(self, p, now):
        x = self.img
        if x is None:
            return self._err(P.IMG_DATA, P.E_STATE)
        off, data = P.img_data_parse(p)
        end = off + len(data)
        if end > x.begin.length:
            return self._err(P.IMG_DATA, P.E_TOO_BIG, x.begin.length)
        if end <= x.next:                      # already written (late retry)
            return self._ack(P.IMG_DATA, P.struct.pack("<I", x.next))
        if off != x.next:
            return self._err(P.IMG_DATA, P.E_OFFSET, x.next)
        if len(data) % 4 and end != x.begin.length:
            return self._err(P.IMG_DATA, P.E_BAD_LEN, 4)
        x.buf[off:end] = data
        x.next = end
        return self._ack(P.IMG_DATA, P.struct.pack("<I", x.next))

    def _img_end(self, p, now):
        x = self.img
        if x is None or x.fw:
            return self._err(P.IMG_END, P.E_STATE)
        if P.img_end_parse(p) != x.begin.frame_id:
            return self._err(P.IMG_END, P.E_BAD_VALUE, 0)
        if x.next != x.begin.length:
            return self._err(P.IMG_END, P.E_OFFSET, x.next)
        s = self.slots[x.slot]
        crc = P.crc32(bytes(s.data))
        self.img = None
        if crc != x.begin.crc32:
            return self._err(P.IMG_END, P.E_CRC, crc)
        s.committed = True
        self.committed_this_session = True
        return self._ack(P.IMG_END)

    def _fw_begin(self, p, now):
        b = P.FwBegin.parse(p)
        if not self.boot_ver:
            return self._err(P.FW_BEGIN, P.E_STATE, P.FWS_NO_BOOT)
        if self.fw_done:
            return self._err(P.FW_BEGIN, P.E_STATE, P.FWS_STAGED)
        if b.load != P.FWU_APP_BASE:
            return self._err(P.FW_BEGIN, P.E_BAD_VALUE, 12)
        if not P.FWU_MIN_IMAGE <= b.length <= P.FWU_MAX_IMAGE or b.length % 4:
            return self._err(P.FW_BEGIN, P.E_TOO_BIG, P.FWU_MAX_IMAGE)
        self.img = None
        if b.fw_version == self.fw_version and len(self.app) >= b.length \
                and P.crc32(self.app[:b.length]) == b.crc32:
            return self._ack(P.FW_BEGIN, bytes([P.IMG_HAVE]))
        # The image overwrites both frame slots; the staging header is
        # programmed with commit and done erased.
        self.slots = [None, None]
        self.staged = None
        self.img = ImgXfer(b, -1, bytearray(b"\xff" * b.length), fw=True)
        return self._ack(P.FW_BEGIN, bytes([P.IMG_START]))

    def _fw_end(self, p, now):
        x = self.img
        if x is None or not x.fw:
            return self._err(P.FW_END, P.E_STATE)
        if P.fw_end_parse(p) != x.begin.crc32:
            return self._err(P.FW_END, P.E_BAD_VALUE, 0)
        if x.next != x.begin.length:
            return self._err(P.FW_END, P.E_OFFSET, x.next)
        self.img = None
        crc = P.crc32(bytes(x.buf))
        if crc != x.begin.crc32:
            return self._err(P.FW_END, P.E_CRC, crc)
        self.staged = (x.begin.fw_version, bytes(x.buf))   # commit word programmed
        self.fw_done = True
        return self._ack(P.FW_END)

    def _hold(self, p, now):
        s = P.hold_parse(p)
        if not 1 <= s <= P.T_HOLD_MAX:
            return self._err(P.HOLD, P.E_BAD_VALUE, 0)
        if self.state == "GRACE":                     # only the BYE grace is left
            return self._err(P.HOLD, P.E_STATE)
        self.holding = True
        self.hold_seen = True
        self.deadline = max(self.deadline, min(now + s, self.t_on + P.T_HOLD_MAX))
        granted = max(0, int(self.deadline - now))
        return self._ack(P.HOLD, P.struct.pack("<H", granted))

    def _bye(self, p, now):
        reason = P.bye_parse(p)
        if self.state != "GRACE":
            if reason == P.BYE_DONE and len(p) >= 2 and p[1] & P.BYE_F_HISTORY_ACK:
                del self.history_hours[:len(self.history_sent)]
            self.result_pending = P.RES_OK if reason in (P.BYE_DONE, P.BYE_FORGOT) else P.RES_NET_FAIL
        self.state = "CLOSING"
        self.closing_at = now

        def after(t):
            self.off_at = t + P.T_BYE_OFF
        return (P.ACK, P.ack_encode(P.BYE), after)

    result_pending: int = P.RES_OK
    holding: bool = False
