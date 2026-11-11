/* Wake events, deep sleep, the WDT feed tick and the power-off path.
 * Register facts and sources: firmware/notes-rtc-power.md.
 *
 * Init order: board_init(), wdt_start(), rtc_init(), power_init().
 * power_init() reads back the WDT period actually in effect (WDT_CON.WOV),
 * so it must run after wdt_start(). */
#pragma once
#include <stdbool.h>
#include <stdint.h>

enum {
    EV_MINUTE = 1,   /* RTC period interrupt, once per minute */
    EV_BTN_TOP = 2,  /* PA10 falling edge */
    EV_BTN_CF = 4,   /* PB04 falling edge */
    EV_SWITCH = 8,   /* PA09 falling edge: power switch moved to off */
    EV_TICK = 16,    /* LPTIM0 tick: the main loop must call wdt_feed() */
    EV_BUSY = 32,    /* PA02 rising edge (only armed inside power_wait_busy) */
};

/* NVIC + GPIO edge interrupts (PA10, PB04, PA09 falling), the LPTIM0 feed
 * tick on RCL, and the RTC NVIC line. */
void power_init(void);

/* Atomically fetch and clear the pending event mask set by the ISRs. */
uint32_t power_take_events(void);

/* OR events into the pending mask. Safe from ISRs and thread code. */
void power_post_events(uint32_t ev);

/* Deep sleep (SLEEPDEEP + WFI) until any interrupt, unless events are
 * already pending. Do not call while UART0 must receive (CBU on): UART0 is
 * clocked from PCLK, which stops in deep sleep. Build with
 * -DPOWER_NO_DEEPSLEEP to use plain sleep instead (keeps SWD attachable). */
void power_sleep(void);

/* Until the first feed tick has been seen, power_sleep/power_idle only wait
 * 100 ms (busy) so the main loop keeps feeding even if LPTIM's first period
 * turns out longer than assumed (review finding 4; verify on hardware). */

/* Sleep until PA02 (e-paper BUSY) is high. Returns at once if it is already
 * high. Gives up after POWER_BUSY_TIMEOUT_MS (measured in feed ticks) and
 * returns false in that case. Feeds the WDT on each tick while waiting. */
bool power_wait_busy(void);

/* Deep sleep on or off at run time (on by default; no effect when built
 * with POWER_NO_DEEPSLEEP). Used for the bench trial, see main.c. */
void power_allow_deep(bool on);

/* Like power_sleep, but plain sleep (SWD stays attachable): debug hold. */
void power_idle(void);

/* Clock helpers shared with rtc.c. */
void power_sysctrl0_set(uint32_t bits, bool on); /* SYSCTRL0 write with the 0x5A5A/0xA5A5 unlock */
bool power_rcl_enable(void); /* RCL on, trimmed to 32.768 kHz; false if STABLE never came */
