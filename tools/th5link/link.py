"""Stop-and-wait link engine shared by the fake CBU and the MCU model
(docs/protocol.md section 3).

An Endpoint owns one side of the byte stream. It is driven by a runner (sim.py
for virtual time, transport.run_realtime for a real port) through four calls:

    feed(data, now)   bytes received
    poll(now)         timers
    take_tx()         bytes to put on the wire
    tx_done(now)      everything taken so far has left the wire

Requests the peer sends are passed to `handler(frame, now)`, which returns
(response_cmd, payload) or (response_cmd, payload, after) where after(t) runs
once that response has been transmitted (used for the baud switch and for
"power off 50 ms after the BYE ACK").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import proto as P


class LinkFailed(Exception):
    """A request got no response after all retries."""


@dataclass
class Outstanding:
    seq: int
    cmd: int
    raw: bytes
    timeout: float
    probe: bool = False            # the one 100 ms baud-probe attempt
    attempts: int = 1
    deadline: float | None = None  # set when the request has left the wire
    on_done: Callable | None = None


@dataclass
class Stats:
    tx_frames: int = 0
    rx_frames: int = 0
    retries: int = 0
    dup_requests: int = 0
    dropped_collisions: int = 0
    reverts: int = 0
    log: list = field(default_factory=list)


class Endpoint:
    def __init__(self, role: str, handler: Callable, rx_max: int, peer_rx_max: int):
        assert role in ("cbu", "mcu")
        self.role = role
        self.handler = handler
        self.parser = P.StreamParser(rx_max)
        self.peer_rx_max = peer_rx_max
        self.baud = P.BAUD_SLOW
        self.sticky_slow = False       # a revert happened: stay at 115200
        self.tx = bytearray()
        self.tx_seq = 0
        self.out: Outstanding | None = None
        self.last_valid_rx: float | None = None
        self.fast_since: float | None = None
        self.dedup: tuple | None = None    # (seq, cmd, crc, response bytes)
        self.after_tx: list[Callable] = []
        self.stats = Stats()
        self.trace: Callable | None = None   # trace(direction, frame, now)

    # --- sending ---------------------------------------------------------
    def _emit(self, raw: bytes, now: float):
        self.tx += raw
        self.stats.tx_frames += 1
        if self.trace:
            self.trace("tx", P.decode_frame(raw), now)

    def request(self, cmd: int, payload: bytes, now: float, on_done: Callable | None = None,
                probe: bool = False) -> int:
        """Start a request. on_done(frame_or_None, now) is called with the
        response, or None after the last retry. Returns the seq used."""
        if self.out is not None:
            raise RuntimeError("stop-and-wait: a request is already outstanding")
        if len(payload) > self.peer_rx_max:
            raise ValueError("payload exceeds the peer's rx_max")
        seq = self.tx_seq
        self.tx_seq = (self.tx_seq + 1) & 0xFF
        raw = P.encode_frame(seq, cmd, payload)
        timeout = (P.T_PROBE - P.T_PROBE_GAP) if probe else P.ack_timeout(cmd)
        self.out = Outstanding(seq, cmd, raw, timeout, probe, on_done=on_done)
        self._emit(raw, now)
        return seq

    def take_tx(self) -> bytes:
        b = bytes(self.tx)
        self.tx.clear()
        return b

    def tx_done(self, now: float):
        if self.out is not None and self.out.deadline is None:
            self.out.deadline = now + self.out.timeout
        hooks, self.after_tx = self.after_tx, []
        for h in hooks:
            h(now)

    # --- baud ------------------------------------------------------------
    def set_baud(self, baud: int, now: float):
        if baud != self.baud:
            self.baud = baud
            self.parser.reset()
            self.fast_since = now if baud == P.BAUD_FAST else None

    def revert(self, now: float):
        """Drop to 115200 for the rest of this session (protocol.md 2.3 R1)."""
        if self.baud != P.BAUD_SLOW:
            self.stats.reverts += 1
        self.sticky_slow = True
        self.set_baud(P.BAUD_SLOW, now)

    def session_reset(self):
        """CBU power-on/off: a new session starts from scratch, at 115200,
        allowed to try 500 k again (protocol.md 2.3)."""
        self.sticky_slow = False
        self.tx_seq = 0
        self.parser = P.StreamParser(self.parser.max_payload)
        self.baud = P.BAUD_SLOW
        self.fast_since = None
        self.tx.clear()
        self.out = None
        self.dedup = None
        self.after_tx = []
        self.last_valid_rx = None

    # --- receiving -------------------------------------------------------
    def feed(self, data: bytes, now: float):
        before = self.parser.discarded
        frames = self.parser.feed(data, now)
        for f in frames:
            self.last_valid_rx = now
            self.stats.rx_frames += 1
            if self.trace:
                self.trace("rx", f, now)
            if P.is_response(f.cmd):
                self._on_response(f, now)
            else:
                self._on_request(f, now)
        if self.parser.discarded > before and self.baud == P.BAUD_FAST:
            ref = self.last_valid_rx if self.last_valid_rx is not None else self.fast_since
            if ref is None or now - ref >= P.T_GARBAGE:
                self.revert(now)                      # R1b

    def _on_response(self, f: P.Frame, now: float):
        o = self.out
        if o is None or f.seq != o.seq:
            return                                    # stale duplicate: ignore
        if f.cmd == P.ACK and (not f.payload or f.payload[0] != o.cmd):
            return
        if f.cmd == P.ERR and (len(f.payload) < 1 or f.payload[0] != o.cmd):
            return
        if f.cmd == P.HELLO_RSP and o.cmd != P.HELLO:
            return
        self.out = None
        if o.on_done:
            o.on_done(f, now)

    def _on_request(self, f: P.Frame, now: float):
        if self.role == "cbu" and self.out is not None:
            self.stats.dropped_collisions += 1        # CBU's own request wins
            return
        crc = P.crc16(P.encode_frame(f.seq, f.cmd, f.payload, f.ver)[1:-2])
        if f.cmd != P.HELLO and self.dedup and self.dedup[:3] == (f.seq, f.cmd, crc):
            self.stats.dup_requests += 1
            self._emit(self.dedup[3], now)            # re-ACK, do not re-apply
            return
        if f.ver != P.VER:
            rsp = (P.ERR, P.Err(f.cmd, P.E_VERSION, P.VER).encode())
        else:
            rsp = self.handler(f, now)
        if rsp is None:
            return
        cmd, payload, *after = rsp
        if len(payload) > self.peer_rx_max:
            raise AssertionError("response exceeds the peer's rx_max")
        raw = P.encode_frame(f.seq, cmd, payload)
        self.dedup = (f.seq, f.cmd, crc, raw)
        self._emit(raw, now)
        if after and after[0]:
            self.after_tx.append(after[0])

    # --- timers ----------------------------------------------------------
    def poll(self, now: float):
        o = self.out
        if o is None or o.deadline is None or now < o.deadline:
            return
        if o.probe:
            # Probe attempt failed: revert, settle, resend the same frame at
            # 115200 with the normal timeout. Not counted as a retry.
            self.revert(now)
            o.probe = False
            o.timeout = P.ack_timeout(o.cmd)
            o.deadline = None
            self._resend_at = now + P.T_SETTLE
            return
        if o.attempts > P.RETRIES:
            self.out = None
            if o.on_done:
                o.on_done(None, now)
            return
        if self.baud == P.BAUD_FAST:
            self.revert(now)                          # R1a
        o.attempts += 1
        o.deadline = None
        self.stats.retries += 1
        self._emit(o.raw, now)

    _resend_at: float | None = None

    def poll_settle(self, now: float):
        if self._resend_at is not None and now >= self._resend_at and self.out is not None:
            self._resend_at = None
            self._emit(self.out.raw, now)

    def next_deadline(self) -> float | None:
        ts = []
        if self.out is not None and self.out.deadline is not None:
            ts.append(self.out.deadline)
        if self._resend_at is not None:
            ts.append(self._resend_at)
        if self.parser.buf and self.parser.last_byte_t is not None:
            ts.append(self.parser.last_byte_t + P.T_INTERBYTE + 1e-6)
        return min(ts) if ts else None

    def tick(self, now: float):
        """Timers, including the inter-byte timeout of a partial frame."""
        if (self.parser.buf and self.parser.last_byte_t is not None
                and now - self.parser.last_byte_t > P.T_INTERBYTE):
            self.feed(b"", now)
        self.poll(now)
        self.poll_settle(now)
