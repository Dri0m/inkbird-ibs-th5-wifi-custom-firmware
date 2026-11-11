/* Pure decision logic for main.c: low battery, the boot-session gate, button
 * gestures, deferred session triggers, the clock period, band and badge.
 * No registers, so test/test_policy.c runs it on the host. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

/* ---- low battery (design 4.6) -------------------------------------------- */

/* Stock firmware's thresholds are in ADC counts (evidence/hc32-re-full.md
 * 11): full 2833, empty 2252, Wi-Fi blocked below 2202, dead below 2115.
 * battery.c anchors the divider on full = 4.20 V, so these are 4.20, 3.34,
 * 3.26 and 3.13 V. */
#define LOWBAT_MV     3400u /* at rest, on battery: "charge me" (above stock's 3.26 V) */
#define LOWBAT_DEAD_MV 3130u /* stock "dead" (2115 counts): power hold dropped */
#define LOWBAT_COUNT  3u    /* consecutive readings below LOWBAT_MV */

typedef struct {
    uint8_t below; /* consecutive on-battery readings below LOWBAT_MV */
    bool low;      /* latched until the charger is connected */
} lowbat_t;

/* One battery check. on_battery: no charger (TC4056A idle); mv: a reading at
 * rest, 0 = none. Charging or full clears the state. Returns s->low. */
bool lowbat_update(lowbat_t *s, bool on_battery, uint16_t mv);

/* ---- boot session gate ------------------------------------------------------ */

/* Minutes to wait before the boot session (no frame or no time), 0 = now.
 * rst: LINK_RST_* bits of this reset. boot_fails: boot sessions started
 * since the last successful session, counted in .noinit RAM across resets.
 * A brown-out, WDT or lockup reset, or repeated boot sessions that never
 * succeed, must not turn into a reset loop that powers the CBU each time. */
uint32_t boot_session_delay_min(uint8_t rst, uint32_t boot_fails);

/* ---- buttons ------------------------------------------------------------------- */

#define BENCH_HOLD_MS   10000u /* top button held: bench session (protocol 4.10) */
#define FACTORY_HOLD_MS 10000u /* both buttons held together: factory reset */

/* ---- deferred session triggers --------------------------------------------- */

#define UNITS_DELAY_MIN 2u   /* C/F press -> UNITS session at the 2nd minute tick (60-120 s) */
#define CHARGE_GAP_MIN  15u  /* no CHARGE session within 15 min of the last session */
#define SCHED_NONE      0xFFu

typedef struct {
    uint32_t due_min;   /* sync countdown in minute ticks, 0 = none */
    uint8_t units_min;  /* UNITS countdown, 0 = none */
    uint8_t charge_min; /* CHARGE countdown, 0 = none */
    uint32_t since_min; /* minutes since the last session ended */
} sched_t;

void sched_units(sched_t *s);   /* C/F pressed: (re)start the UNITS countdown */
void sched_charge(sched_t *s);  /* charge state changed */
/* After any session: next sync in due_min ticks; pending UNITS/CHARGE are
 * served by it. */
void sched_done(sched_t *s, uint32_t due_min);
/* One minute tick. Returns the LINK_TRIG_* to run now, or SCHED_NONE.
 * auto_on: automatic sessions allowed (not on the bench, not low battery).
 * last_ok: the last session succeeded (TIMER, else RETRY). */
uint8_t sched_minute(sched_t *s, bool auto_on, bool last_ok);

/* ---- display -------------------------------------------------------------------- */

/* CLOCK_MIN: the band's clock and sensors update on minutes where
 * (hour*60 + min) is a multiple of clock_min. */
bool clock_tick(uint8_t hour, uint8_t min, uint8_t clock_min);

/* MODE 1 (forecast) with a band frame: live band updates. MODE 0 (picture):
 * the frame is shown as is, no band, no per-minute sensor reads. */
bool band_live(uint8_t mode, bool frame_has_band);

/* FLAGS bit 0 BADGE in picture mode: show the battery/Wi-Fi icons over the
 * picture's top-right corner when the link failed or the battery is low. */
#define BADGE_BATT_PCT 10u
bool badge_on(uint8_t mode, uint8_t flags, uint8_t link_icon, uint8_t batt_pct);
