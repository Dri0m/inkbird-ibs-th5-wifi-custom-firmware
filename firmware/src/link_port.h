/* Byte transport under the link engine (link.c). One implementation is
 * linked in:
 *   uart.c   UART0 on PB06/PB07 with the CBU powered through PB05 (default)
 *   mbox.c   the SWD mailbox `link_mbox` (make LINK_MBOX=1, bench builds;
 *            docs/protocol.md 8). PB05 stays high: the real CBU is off.
 *   test/link_host.c  the host test harness
 * The frame engine above is identical for all of them. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Once at boot (mbox: publish the mailbox; uart: nothing). */
void lport_init(void);

/* CBU power. On: pins to UART, RX buffer cleared, RX on, baud 115200, then
 * PB05 low (protocol.md 2.2). Off: PB05 high, UART off, pins parked low.
 * HCLK must already be 24 MHz for the UART (link.c switches it). */
void lport_power(bool on);

/* Next received byte, or -1. */
int lport_getc(void);

/* Queue/transmit n bytes. May block until they are in the transmitter; the
 * bytes of one call are never interleaved with anything else. */
void lport_write(const uint8_t *p, uint32_t n);

/* True once every byte written has fully left the wire (UART TC: the last
 * stop bit is out; mbox: the bytes are in the ring, protocol.md 8.3). */
bool lport_tx_idle(void);

/* Line rate from now on (115200 or 500000). Only called while tx idle. */
void lport_set_baud(uint32_t baud);
