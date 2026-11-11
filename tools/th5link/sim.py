"""Virtual-time loopback of the MCU model and the fake CBU.

The wire carries each written chunk as one unit, taking 10 bit times per byte
at the sender's baud rate. If the receiver's rate differs at delivery, it gets
garbage of the same length (0x00 bytes: a UART at the wrong rate mostly sees
framing errors and zeros). A `fault(direction, data, now)` hook can drop or
alter chunks: return None to drop, or the bytes to deliver.
"""
from __future__ import annotations

import heapq
import itertools
from typing import Callable

from . import proto as P
from .fakecbu import CbuScript, FakeCbu
from .mcumodel import McuModel


class Sim:
    def __init__(self, mcu: McuModel, cbu_factory: Callable[[], FakeCbu],
                 fault: Callable | None = None):
        self.mcu = mcu
        self.cbu_factory = cbu_factory
        self.cbu: FakeCbu | None = None
        self.cbus: list[FakeCbu] = []
        self.fault = fault
        self.now = 0.0
        self.q: list = []                    # (t, n, kind, args)
        self.n = itertools.count()
        self.line_free = {"m2c": 0.0, "c2m": 0.0}
        self.trace: list[tuple] = []         # (t, direction, frame)
        self.cbu_boot_at: float | None = None
        self.power_was = False

    def _push(self, t, kind, *args):
        heapq.heappush(self.q, (t, next(self.n), kind, args))

    def _send(self, direction: str, node, data: bytes):
        if not data:
            return
        start = max(self.now, self.line_free[direction])
        t = start + len(data) * 10 / node.baud
        self.line_free[direction] = t
        self._push(t, "deliver", direction, data, node.baud, node)

    def reboot_cbu(self, delay: float = 0.0):
        """Simulate a CBU crash/reboot while powered (e.g. during HOLD)."""
        self.cbu = None
        self.cbu_boot_at = self.now + delay

    def _drain(self):
        if self.mcu.cbu_on:
            self._send("m2c", self.mcu, self.mcu.take_tx())
        if self.cbu is not None:
            self._send("c2m", self.cbu, self.cbu.take_tx())

    def _power(self):
        on = self.mcu.cbu_on
        if on and not self.power_was:
            self.cbu = None
            self.cbu_boot_at = self.now
        if not on and self.power_was:
            self.cbu = None
            self.cbu_boot_at = None
        self.power_was = on

    def step(self) -> bool:
        cands = []
        if self.q:
            cands.append(self.q[0][0])
        for node in (self.mcu, self.cbu):
            d = node.next_deadline() if node is not None else None
            if d is not None:
                cands.append(d)
        if self.cbu_boot_at is not None:
            cands.append(self.cbu_boot_at)
        if not cands:
            return False
        self.now = max(self.now, min(cands))
        if self.cbu_boot_at is not None and self.now >= self.cbu_boot_at:
            self.cbu_boot_at = None
            self.cbu = self.cbu_factory()
            self.cbus.append(self.cbu)
            self.cbu.ep.trace = lambda d, f, t: self.trace.append((t, "cbu-" + d, f))
            self.cbu.start(self.now)
        while self.q and self.q[0][0] <= self.now:
            _, _, kind, args = heapq.heappop(self.q)
            direction, data, baud, sender = args
            if sender is self.mcu and not self.mcu.cbu_on:
                continue
            if sender is not self.mcu and sender is not self.cbu:
                continue                    # sender was powered off / rebooted
            sender.tx_done(self.now)
            rx = self.cbu if direction == "m2c" else self.mcu
            if rx is None:
                continue
            if self.fault:
                data = self.fault(direction, data, self.now)
                if data is None:
                    continue
            if rx.baud != baud:
                data = bytes(len(data))
            rx.feed(data, self.now)
        self.mcu.poll(self.now)
        if self.cbu is not None:
            self.cbu.poll(self.now)
        self._power()
        self._drain()
        return True

    def run_session(self, trigger=P.TRIG_TIMER, bench=False, limit=1000.0) -> int | None:
        """Start a session and run until the MCU powers the CBU off."""
        self.mcu.ep.trace = lambda d, f, t: self.trace.append((t, "mcu-" + d, f))
        if not self.mcu.start_session(self.now, trigger, bench):
            return None
        self._power()
        end = self.now + limit
        while self.mcu.cbu_on and self.now < end:
            if not self.step():
                break
        return self.mcu.result


def frames(sim: Sim, who: str = "mcu-rx") -> list[P.Frame]:
    return [f for _, d, f in sim.trace if d == who]


def default_sim(**script_kw) -> Sim:
    mcu = McuModel()
    return Sim(mcu, lambda: FakeCbu(CbuScript(**script_kw)))
