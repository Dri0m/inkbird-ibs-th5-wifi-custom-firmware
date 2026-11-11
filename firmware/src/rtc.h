/* Real-time clock on the 32.768 kHz crystal (XTL, PC14/PC15).
 * Register facts and sources: firmware/notes-rtc-power.md. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    uint8_t year;  /* 0-99, meaning 2000-2099 */
    uint8_t month; /* 1-12 */
    uint8_t day;   /* 1-31 */
    uint8_t wday;  /* 0 = Sunday .. 6 = Saturday */
    uint8_t hour;  /* 0-23 */
    uint8_t min;   /* 0-59 */
    uint8_t sec;   /* 0-59 */
} rtc_time_t;

/* Enable the XTL (bounded wait, up to about 5 s on a cold start) and run the
 * RTC from it in 24 h mode, with a period interrupt once per minute (the
 * RTC_IRQHandler posts EV_MINUTE; power_init enables the NVIC line).
 * If the RTC was already counting (warm reset), its time is kept.
 * Returns false if the XTL did not start: the RTC then runs from RCL
 * (about +-3 %, so the clock drifts minutes per day) until the next boot. */
bool rtc_init(void);

/* True if rtc_init found the RTC already counting, so the time survived the
 * reset and does not need to be set before the first TIME sync. */
bool rtc_running_before(void);

void rtc_get(rtc_time_t *t); /* consistent snapshot (WAIT lock), BCD -> binary */
void rtc_set(const rtc_time_t *t);
