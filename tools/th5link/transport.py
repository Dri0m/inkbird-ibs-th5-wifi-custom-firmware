"""Byte transports for the link and a real-time runner (docs/protocol.md 2, 8).

    SerialTransport      pyserial; a USB-UART adapter on the MCU's UART0 pads
    SwdMailboxTransport  pyocd; the `link_mbox` RAM rings of a bench MCU build

Both carry exactly the same byte stream. pyserial and pyocd are imported
lazily, so the rest of th5link stays stdlib-only.

Run against the TH5 bench MCU (firmware/notes-link.md). The SWD transport attaches without halting or
resetting the core and only ever writes the host-owned words of link_mbox
(h2m data, h2m_head, m2h_tail, host_ctl).
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import time
from pathlib import Path

from . import proto as P

NM = Path.home() / "opt/arm-gnu-toolchain-15.3.rel2-darwin-arm64-arm-none-eabi/bin/arm-none-eabi-nm"
TARGET = "hc32l170faua"


class SerialTransport:
    """The adapter stands in for the CBU. There is no power signal on the UART,
    so wait_power() returns at once: start a session on the MCU (top button, or
    bench mode) and run the fake CBU within the 5 s HELLO window, or use
    CbuScript.hello_wait to keep sending HELLO."""

    def __init__(self, port: str):
        import serial  # pyserial
        self.ser = serial.Serial(port, P.BAUD_SLOW, timeout=0)
        self.baud = P.BAUD_SLOW

    def wait_power(self, timeout: float) -> bool:
        return True

    def power_on(self) -> bool:
        return True

    def read(self, timeout: float) -> bytes:
        self.ser.timeout = max(timeout, 0)
        b = self.ser.read(1)
        if b:
            self.ser.timeout = 0
            b += self.ser.read(4096)
        return b

    def write(self, data: bytes):
        self.ser.write(data)

    def flush(self):
        self.ser.flush()                     # waits until the OS has sent it

    def set_baud(self, baud: int):
        self.ser.baudrate = baud
        self.baud = baud

    def close(self):
        self.ser.close()


def find_symbol(elf: str, name: str = "link_mbox") -> int:
    nm = str(NM) if NM.exists() else shutil.which("arm-none-eabi-nm")
    if not nm:
        raise FileNotFoundError("arm-none-eabi-nm not found")
    out = subprocess.run([nm, elf], check=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == name:
            return int(parts[0], 16)
    raise KeyError(f"{name} not in {elf} (built without -DLINK_MBOX?)")


class SwdMailboxTransport:
    """protocol.md 8: the host is the CBU. It owns h2m_head, m2h_tail and
    host_ctl; the MCU owns everything else. Data is written before the index
    that publishes it; indices are single aligned 32-bit accesses."""

    POLL = 0.002

    def __init__(self, elf: str, pack: str, unique_id: str | None = None):
        from pyocd.core.helpers import ConnectHelper
        self.base = find_symbol(elf)
        self.session = ConnectHelper.session_with_chosen_probe(
            unique_id=unique_id, target_override=TARGET, connect_mode="attach",
            options={"pack": pack, "resume_on_disconnect": True, "frequency": 4000000})
        self.session.open()
        self.t = self.session.board.target
        self.wait_retries = 0
        self.baud = P.BAUD_SLOW
        magic = self._r(P.MBOX_OFF["magic"])
        ver_size = self._r(P.MBOX_OFF["version"])
        if magic != P.MBOX_MAGIC or ver_size != (P.MBOX_VERSION | P.MBOX_RING << 16):
            self.close()
            raise RuntimeError(f"link_mbox at 0x{self.base:08x}: magic 0x{magic:08x}, "
                               f"version/size 0x{ver_size:08x}: firmware not in mailbox mode")
        self.booted = False

    def _retry(self, fn, *a):
        """An SWD access can get WAIT ACKs while the MCU erases flash (instruction
        fetch and the bus stall ~4.6 ms) or steps its clock: retry a few times.
        Each retried access is idempotent (same value, same address)."""
        from pyocd.core.exceptions import TransferError
        for i in range(20):
            try:
                return fn(*a)
            except TransferError:
                if i == 19:
                    raise
                self.wait_retries += 1
                time.sleep(0.005)

    def _wsync(self, fn, *a):
        """A write and the flush that completes it, retried as one unit: pyOCD may
        queue the write and report its WAIT/fault only at the flush, so retrying
        the flush alone could drop the write."""
        def once():
            fn(*a)
            self.t.flush()
        self._retry(once)

    def _r(self, off: int) -> int:
        return self._retry(self.t.read32, self.base + off)

    def _w(self, off: int, v: int):
        self._wsync(self.t.write32, self.base + off, v)

    def request_session(self, elf: str, trigger: int):
        """Bench builds: ask the MCU to start a session (main.c link_bench_req)."""
        addr = find_symbol(elf, "link_bench_req")
        self._wsync(self.t.write32, addr, 0xB5E55000 | (trigger & 0xFF))

    def power_on(self) -> bool:
        on = bool(self._r(P.MBOX_OFF["mcu_ctl"]) & P.MBOX_MCU_CBU_ON)
        if not on and self.booted:
            self._w(P.MBOX_OFF["host_ctl"], 0)  # "power cut": the CBU is gone
            self.booted = False
        return on

    def wait_power(self, timeout: float) -> bool:
        """Wait for the MCU to 'power the CBU', then boot: drop stale MCU
        output and set host_ctl.BOOTED."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._r(P.MBOX_OFF["mcu_ctl"]) & P.MBOX_MCU_CBU_ON:
                self._w(P.MBOX_OFF["m2h_tail"], self._r(P.MBOX_OFF["m2h_head"]))
                self._w(P.MBOX_OFF["host_ctl"], P.MBOX_HOST_BOOTED)
                self.booted = True
                return True
            time.sleep(0.01)
        return False

    def read(self, timeout: float) -> bytes:
        end = time.monotonic() + timeout
        while True:
            head = self._r(P.MBOX_OFF["m2h_head"])
            tail = self._r(P.MBOX_OFF["m2h_tail"])
            if head != tail:
                if head >= P.MBOX_RING or tail >= P.MBOX_RING:
                    raise RuntimeError("link_mbox m2h index out of range")
                base = self.base + P.MBOX_OFF["m2h_data"]
                if head > tail:
                    data = bytes(self._retry(self.t.read_memory_block8, base + tail, head - tail))
                else:
                    data = bytes(self._retry(self.t.read_memory_block8, base + tail, P.MBOX_RING - tail))
                    data += bytes(self._retry(self.t.read_memory_block8, base, head))
                self._w(P.MBOX_OFF["m2h_tail"], head)   # after the data was read
                return data
            if time.monotonic() >= end:
                return b""
            time.sleep(self.POLL)

    def write(self, data: bytes):
        base = self.base + P.MBOX_OFF["h2m_data"]
        while data:
            head = self._r(P.MBOX_OFF["h2m_head"])
            tail = self._r(P.MBOX_OFF["h2m_tail"])
            free = (tail - head - 1) % P.MBOX_RING
            if not free:
                time.sleep(self.POLL)
                continue
            n = min(free, len(data), P.MBOX_RING - head)
            # data reaches SRAM (flushed, retried with the write) before the index
            self._wsync(self.t.write_memory_block8, base + head, list(data[:n]))
            self._w(P.MBOX_OFF["h2m_head"], (head + n) % P.MBOX_RING)
            data = data[n:]

    def flush(self, timeout: float = 0.5):
        """'TX complete' = the MCU has consumed everything written."""
        self._retry(self.t.flush)
        end = time.monotonic() + timeout
        while (self._r(P.MBOX_OFF["h2m_tail"]) != self._r(P.MBOX_OFF["h2m_head"])
               and time.monotonic() < end):
            time.sleep(self.POLL)

    def set_baud(self, baud: int):
        self.baud = baud                      # no-op on the mailbox

    def close(self):
        try:
            if getattr(self, "booted", False):
                self._w(P.MBOX_OFF["host_ctl"], 0)
        finally:
            self.session.close()


def run_realtime(node, tr, timeout: float = 60.0):
    """Drive a node (FakeCbu, or anything with the runner interface) over a
    transport in wall-clock time."""
    t0 = time.monotonic()
    if not tr.wait_power(timeout):
        raise TimeoutError("the MCU never powered the CBU")
    now = lambda: time.monotonic() - t0  # noqa: E731
    node.start(now())

    def sync_baud():
        if node.baud != tr.baud:
            tr.set_baud(node.baud)
    while not getattr(node, "done", False) and now() < timeout:
        if not tr.power_on():
            break
        d = node.next_deadline()
        wait = 0.005 if d is None else min(max(d - now(), 0.0), 0.005)
        data = tr.read(wait)
        if data:
            node.feed(data, now())
        node.poll(now())
        sync_baud()
        tx = node.take_tx()
        if tx:
            tr.write(tx)
            tr.flush()
            node.tx_done(now())
            sync_baud()
