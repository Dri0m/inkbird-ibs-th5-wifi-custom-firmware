# /// script
# requires-python = ">=3.10"
# dependencies = ["pyocd==0.46.*", "pyserial"]
# ///
"""Pause the HC32 and keep the CBU powered, so the CBU's flash UART is free.

Holding the HC32 in reset doesn't work: PB05 floats and the CBU loses power.
Instead this halts the running firmware over SWD and then sets three pins with
the same GPIO registers the firmware uses (see evidence/hc32-re-hardware.md):

    PB06/PB07 (HC32 UART) -> plain GPIO inputs, so they don't fight the adapter
    PB05 (CBU power, active-low) -> low, so the module is powered

Only GPIO registers are written. Flash is never touched. On exit the HC32 is
reset, so whatever firmware is in its flash (stock or ours) starts again.

    uv run tools/cbu_hold.py                 # interactive: c = power-cycle CBU, q = quit
    uv run tools/cbu_hold.py --port PORT -- CMD ARGS...  # hold, run CMD, reset the CBU
                                             # through RTS when ltchiptool asks for a reboot
    uv run tools/cbu_hold.py --manual -- CMD ARGS...  # same, but you reset the CBU
                                                      # yourself (tap CEN to GND)

The command forms are for ltchiptool, which needs the CBU to reboot while it's
trying to connect. A power cycle doesn't reset the CBU on this board: its rail
only sags to about 2 V while switched off. Pulling CEN (module pin 18) low does,
and CEN is wired to the UART adapter's RTS pin.

ltchiptool opens the port with pyserial's default RTS asserted, which holds
CEN low (the chip in reset) for its whole link attempt; its own RTS pulse is
undone by its reopen. So once it starts waiting, this script opens a second
handle on the same port and releases RTS, pulsing it again every few seconds
until the link is up. Without --manual, --port must name the adapter.
"""

import os
import subprocess
import sys
import threading
import time

import serial
from pyocd.core.helpers import ConnectHelper
from pyocd.core.target import Target

# HC32L17x GPIO, port B = port A + 0x40 (from the firmware's Gpio_Init at 0x3410
# and its set/clear helpers at 0x34E0/0x33C4).
PB_SEL = 0x40020C40  # one 32-bit function-select word per pin
PBDIR = 0x40020D40   # 1 = input
PBIN = 0x40020D44
PBBSET = 0x40020D50
PBBCLR = 0x40020D54

PB05, PB06, PB07 = 1 << 5, 1 << 6, 1 << 7


def cbu_power(target, on):
    target.write32(PBBCLR if on else PBBSET, PB05)


def main():
    with ConnectHelper.session_with_chosen_probe(
        target_override="cortex_m", options={"connect_mode": "attach"}
    ) as session:
        target = session.target
        target.halt()
        print("HC32 halted. Checking the watchdog doesn't reset it (3 s)...")
        time.sleep(3)
        if target.get_state() != Target.State.HALTED:
            print("The HC32 is no longer halted (the watchdog probably reset it). Stopping; nothing was changed.")
            target.reset()
            return
        print("Still halted, so the watchdog is paused while debugging.")

        target.write32(PB_SEL + 6 * 4, 0)
        target.write32(PB_SEL + 7 * 4, 0)
        target.write32(PBDIR, target.read32(PBDIR) | PB06 | PB07)
        cbu_power(target, True)
        print(f"PB06/PB07 are now inputs; CBU power is on (PBIN={target.read32(PBIN):#06x}, bit 5 should be 0).")
        cmd = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else None
        try:
            if cmd:
                run_with_reboot(target, cmd)
            else:
                interactive(target)
        finally:
            target.reset()
            print("HC32 reset; it runs whatever firmware is in its flash again.")


def run_with_reboot(target, cmd):
    """Run cmd, resetting the CBU once cmd asks for a reboot.

    ltchiptool ends its "To enter download mode..." prompt with a line about
    bridging CEN to GND, then starts trying to link. From then on, pulse RTS
    every few seconds until it prints anything else, which means the link is up.
    """
    own_args = sys.argv[1: sys.argv.index("--")]
    manual = "--manual" in own_args
    port = own_args[own_args.index("--port") + 1] if "--port" in own_args else None
    if not manual and not port:
        sys.exit("cbu_hold: pass --port PORT (the adapter whose RTS is wired to CEN) or --manual")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    waiting = threading.Event()

    def rts_resetter():
        waiting.wait()
        # Let ltchiptool finish reopening the port (which asserts RTS) first.
        time.sleep(1)
        s = serial.Serial()
        s.port, s.baudrate = port, 115200
        s.rts = s.dtr = False  # opening with these releases CEN: the CBU boots
        s.open()
        print("cbu_hold: CEN released through RTS", flush=True)
        while proc.poll() is None:
            time.sleep(3)
            if not waiting.is_set():
                continue
            s.rts = True
            time.sleep(0.1)
            s.rts = False
            print("cbu_hold: CBU reset through RTS", flush=True)
        s.close()

    if not manual:
        threading.Thread(target=rts_resetter, daemon=True).start()
    for line in proc.stdout:
        print(line, end="", flush=True)
        if "bridge CEN" in line:
            waiting.set()
            if manual:
                print("cbu_hold: reset the CBU now (one short tap of CEN to GND)", flush=True)
        elif waiting.is_set() and line.strip() not in ("I:", ""):
            waiting.clear()
    rc = proc.wait()
    if target.get_state() != Target.State.HALTED:
        print("cbu_hold: warning, the HC32 was running again by the end.")
    if rc:
        sys.exit(rc)


def interactive(target):
    print("Commands: c = power-cycle the CBU (for ltchiptool), off / on = CBU power, q = reset the HC32 and quit")
    while True:
        cmd = input("> ").strip().lower()
        if cmd in ("off", "on"):
            cbu_power(target, cmd == "on")
            print(f"CBU power {cmd}.")
        elif cmd == "c":
            cbu_power(target, False)
            time.sleep(0.3)
            cbu_power(target, True)
            print("CBU power-cycled.")
        elif cmd == "q":
            break
        if target.get_state() != Target.State.HALTED:
            print("Warning: the HC32 is running again.")


if __name__ == "__main__":
    main()
