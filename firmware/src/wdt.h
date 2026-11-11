/* Watchdog. Once started it cannot be stopped, and it survives a debugger
 * (SYSRESETREQ) reset. Its bus clock (PERI_CLKEN0 bit 15) is gated off by
 * every reset, and with the gate off feeds are silently dropped while the
 * counter keeps running (evidence/m0-display-tests.md). */
#pragma once
#include <stdint.h>

void wdt_start(void); /* gate on, set the period, reset mode, start */
void wdt_feed(void);  /* main loop only, never from an ISR */
