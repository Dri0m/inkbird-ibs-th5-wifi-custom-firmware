/* Clock, thermometer, the stored frame with the live band (or the fallback
 * screen when no valid frame is stored), and CBU link sessions (link.c,
 * docs/protocol.md 5) that bring time, settings and new frames.
 *
 * Boot: power hold, 2 s awake so SWD can always attach, then the
 * event loop sleeps between RTC minute wakes, WDT feed ticks and buttons.
 * Holding the top button during boot keeps the MCU out of deep sleep.
 *
 * Sessions: the top button (one beep; held 10 s: two beeps and bench
 * mode, HOLD 900; another press cancels a running session, one beep; no
 * beep when low battery blocks sessions), the C/F button (a UNITS session
 * 1-2 min later), a charge-state change, the sync schedule (settings SYNC_MIN, backoff after failures) and, at boot, a
 * missing frame or time (gated against reset loops). A session is a busy
 * loop at 24 MHz (no sleep at all) until the CBU is off again; the panel is
 * only refreshed afterwards (design 3 step 10). Both buttons held 10 s:
 * three beeps and factory reset (settings and slot headers erased, FORGET next).
 *
 * Low battery (design 4.6): 3 readings in a row below 3.40 V on battery
 * stop all sessions and band updates and leave a "charge me" screen until
 * the charger is connected. Below stock's "dead" point after that, the
 * power hold (PA08) is dropped: off until the switch or charger restarts it.
 * Bench builds (make LINK_MBOX=1) talk to a fake CBU over SWD instead of
 * UART0, never power the real CBU, run no automatic sessions, and take a
 * session request from SWD (`link_bench_req`, notes-link.md). */
#include <stdbool.h>
#include <stdint.h>

#include "battery.h"
#include "board.h"
#include "epd.h"
#include "flash.h"
#include "frame.h"
#include "link.h"
#include "policy.h"
#include "power.h"
#include "render.h"
#include "rtc.h"
#include "history.h"
#include "settings.h"
#include "sht40.h"
#include "wdt.h"
#ifdef LINK_MBOX
#include "bench.h"
#endif

/* Automatic sessions (schedule, boot). Off on the bench: the mailbox has
 * no CBU behind it unless the host runs fakecbu. */
#ifndef LINK_AUTO
#ifdef LINK_MBOX
#define LINK_AUTO 0
#else
#define LINK_AUTO 1
#endif
#endif

#define GREY_MIN_T10 50  /* below 5 degC greys are unreliable: plane0 only (design 5.2) */
#define GREY_HYST_T10 5  /* +-0.5 degC around it, so a reading at 5.0 does not flip modes */
#define NO_REFRESH_T10 0 /* below 0 degC the panel is not refreshed at all */

/* Bench trial: deep sleep only for the first N minutes after boot, then plain
 * sleep, so SWD access is guaranteed to come back even if deep sleep broke it.
 * make DEEPSLEEP_TRIAL=N; 0 or unset = no limit. */
#ifndef DEEPSLEEP_TRIAL_MIN
#define DEEPSLEEP_TRIAL_MIN 0u
#endif

static screen_t shown;  /* what is on the glass now */
static screen_t want;   /* what should be */
static bool shown_valid;
static uint32_t fast_count;
static bool debug_hold;
static const frame_hdr_t *frame; /* NULL: fallback screen */
static uint16_t batt_mv;          /* on-battery reading at rest, 0 = none now */
static lowbat_t lowbat;
static bool lowbat_shown;         /* "charge me" is on the glass: no more refreshes */
static bool shown_badge;          /* picture-mode badge on the glass */
static bool shown_panel_grey;     /* full frame waveform currently on glass */
static bool shown_grey;           /* the glass shows the band in 4 greys (anti-aliased) */
static charge_state_t last_cs;

/* Session triggers and the sync countdown (policy.h). */
static sched_t link_sched;

/* Survives resets other than power-on (not zeroed by startup.c): boot
 * sessions started since the last successful session, so a reset loop does
 * not power the CBU on every boot (policy.h boot_session_delay_min). */
#define NV_MAGIC 0x4E563554u
__attribute__((section(".noinit"))) static struct {
    uint32_t magic;
    uint32_t boot_fails;
} nv;
/* Bench: the host writes LINK_BENCH_REQ | trigger to start a session. */
volatile uint32_t link_bench_req;
#define LINK_BENCH_REQ 0xB5E55000u

typedef struct {
    const uint8_t *plane; /* NULL: white outside the band */
    const screen_t *s;
    bool band;
    bool badge;           /* picture mode: icons over the top-right corner */
    bool lo;              /* the band's lo plane (grey DTM2), else its hi plane */
} frame_row_t;

typedef struct {
    const screen_t *s;
    bool lo;
    bool band_mono;
} fallback_row_t;

#define BADGE_X0 375u     /* band rows from here hold only the icons (render.c) */

static const settings_t *cfg(void) { return settings_get(); }
static void beep(uint32_t n);

/* link.c's verdict is shown as is (want.status = o->status). */
_Static_assert((int)LINK_STATUS_NONE == (int)RENDER_ST_NONE &&
               (int)LINK_STATUS_NOT_SET_UP == (int)RENDER_ST_NOT_SET_UP &&
               (int)LINK_STATUS_LINK_ERROR == (int)RENDER_ST_LINK_ERROR &&
               (int)LINK_STATUS_WIFI == (int)RENDER_ST_WIFI &&
               (int)LINK_STATUS_HA_UNREACH == (int)RENDER_ST_HA_UNREACH &&
               (int)LINK_STATUS_HA_NO_ANSWER == (int)RENDER_ST_HA_NO_ANSWER,
               "LINK_STATUS_* must equal RENDER_ST_*");

static void row_fallback(void *ctx, uint16_t row, uint8_t out[EPD_ROW_BYTES])
{
    const fallback_row_t *c = ctx;
    uint8_t other[EPD_ROW_BYTES];
    if (c->lo) {
        render_fallback_row(c->s, row, other, out);
        if (c->band_mono) for (uint32_t i = 0; i < 7; i++) out[i] = other[i];
    }
    else render_fallback_row(c->s, row, out, other);
}

static void row_frame(void *ctx, uint16_t row, uint8_t out[EPD_ROW_BYTES])
{
    const frame_row_t *c = ctx;
    const uint8_t *src = c->plane ? c->plane + row * EPD_ROW_BYTES : 0;
    for (uint32_t i = 0; i < EPD_ROW_BYTES; i++) out[i] = src ? src[i] : 0xFF;
    if (c->band || (c->badge && row >= BADGE_X0)) {
        uint8_t hi[7], lo[7]; /* bytes 0..6 */
        render_band_row(c->s, row, hi, lo);
        const uint8_t *b = c->lo ? lo : hi;
        for (uint32_t i = 0; i < 7; i++) out[i] = b[i];
    }
}

/* Grey band update (two passes, epd.c LUT_ERASE / LUT_DRAW). Each pass codes
 * the changed band pixels with a level, everything else 11 (undriven):
 * ERASE with the old level where the old pixel was not white, DRAW with the
 * new level where the new one is not white. One sweep renders the old and
 * the new band row (the renderer caches both layouts), streams the hi plane
 * to DTM1 and keeps the lo plane here for DTM2. */
static uint8_t band_lo[EPD_ROWS][7];

typedef struct {
    bool draw;
    bool any; /* some pixel is driven */
} band_pass_t;

static void row_band_pass(void *ctx, uint16_t row, uint8_t out[EPD_ROW_BYTES])
{
    band_pass_t *p = ctx;
    uint8_t oh[7], ol[7], nh[7], nl[7];
    render_band_row(&shown, row, oh, ol);
    render_band_row(&want, row, nh, nl);
    for (uint32_t k = 0; k < 7; k++) {
        uint8_t changed = (uint8_t)((oh[k] ^ nh[k]) | (ol[k] ^ nl[k]));
        uint8_t h = p->draw ? nh[k] : oh[k], l = p->draw ? nl[k] : ol[k];
        uint8_t m = (uint8_t)(changed & ~(h & l)); /* changed and not white */
        if (m) p->any = true;
        out[k] = (uint8_t)(h | ~m);
        band_lo[row][k] = (uint8_t)(l | ~m);
    }
    for (uint32_t k = 7; k < EPD_ROW_BYTES; k++) out[k] = 0xFF;
}

static void row_band_lo(void *ctx, uint16_t row, uint8_t out[EPD_ROW_BYTES])
{
    (void)ctx;
    for (uint32_t k = 0; k < EPD_ROW_BYTES; k++) out[k] = k < 7 ? band_lo[row][k] : 0xFF;
}

/* One pass; *ran: a refresh was started (the panel needs a reset before the
 * next pass). */
static bool band_pass(bool draw, bool *ran)
{
    band_pass_t p = {draw, false};
    epd_plane(EPD_DTM1, row_band_pass, &p);
    *ran = p.any;
    if (!p.any) return true;
    epd_plane(EPD_DTM2, row_band_lo, 0);
    return epd_refresh(draw ? LUT_DRAW : LUT_ERASE);
}

/* Band update, the glass known to show `shown`. Grey: erase, then draw.
 * Black and white: table B with the hi planes (only changed pixels are
 * driven), white outside the band. */
static bool band_update(void)
{
    if (!shown_grey) {
        frame_row_t was = {0, &shown, true, false, false}, now = {0, &want, true, false, false};
        epd_plane(EPD_DTM1, row_frame, &was);
        epd_plane(EPD_DTM2, row_frame, &now);
        return epd_refresh(LUT_FAST);
    }
    bool ran;
    bool ok = band_pass(false, &ran);
    if (ran && !(cfg()->v[LINK_CFG_FLAGS] & LINK_CFGF_BAND_SINGLE_RESET)) epd_begin();
    return band_pass(true, &ran) && ok;
}

static bool same_screen(const screen_t *a, const screen_t *b)
{
    return a->t10 == b->t10 && a->rh10 == b->rh10 && a->fahrenheit == b->fahrenheit &&
           a->sensor_ok == b->sensor_ok && a->time_valid == b->time_valid &&
           a->hour == b->hour && a->min == b->min && a->wday == b->wday &&
           a->day == b->day && a->month == b->month &&
           a->battery_pct == b->battery_pct && a->status == b->status &&
           a->link == b->link && a->charging == b->charging && a->h12 == b->h12;
}

/* Greys need the panel above GREY_MIN_T10; below it everything is B/W. The
 * threshold moves by GREY_HYST_T10 away from what the glass shows, so a
 * reading that hovers around it does not force a full refresh each minute. */
static bool grey_ok(void)
{
    int16_t min = shown_panel_grey ? GREY_MIN_T10 - GREY_HYST_T10 : GREY_MIN_T10 + GREY_HYST_T10;
    return !(want.sensor_ok && want.t10 < min);
}

static bool band_grey(void)
{
    return grey_ok() && !(cfg()->v[LINK_CFG_FLAGS] & LINK_CFGF_BAND_MONO);
}

/* Full refresh with the 4-grey LUT (table A, hi planes only, in the cold).
 * f: the stored frame (design 5.3), both planes from flash with the band
 * replaced by the MCU's render (a 1 bpp frame sends plane0 twice, so it
 * shows in black and white with a grey band); a picture (no live band)
 * draws its optional badge here only. f NULL: the fallback screen. */
static bool show_full(const frame_hdr_t *f, bool band, bool badge)
{
    bool grey = grey_ok(), bgrey = band_grey();
    if (f) {
        frame_row_t p0 = {frame_plane(f, 0), &want, band, badge, false};
        frame_row_t p1 = {frame_plane(f, grey && f->bpp == 2u ? 1u : 0u), &want, band, badge,
                          bgrey};
        epd_plane(EPD_DTM1, row_frame, &p0);
        epd_plane(EPD_DTM2, row_frame, &p1);
    } else {
        fallback_row_t p0 = {&want, false, false}, p1 = {&want, grey, !bgrey};
        epd_plane(EPD_DTM1, row_fallback, &p0);
        epd_plane(EPD_DTM2, row_fallback, &p1);
    }
    shown_panel_grey = grey;
    shown_grey = bgrey;
    fast_count = 0;
    return epd_refresh(grey ? LUT_GREY : LUT_FULL);
}

/* A live band (or the fallback screen, which is all live). */
static bool live(void)
{
    return !frame || band_live((uint8_t)cfg()->v[LINK_CFG_MODE], frame->flags & FRAME_FLAG_BAND);
}

static bool cold(void) { return want.sensor_ok && want.t10 < NO_REFRESH_T10; }

static void show(bool full)
{
    if (lowbat_shown) return; /* "charge me" stays until the charger is connected */
    if (cold()) {
        /* Glass unchanged. A full refresh asked for now (a new frame) must
         * not be lost: mark the glass unknown so the next one is full. */
        if (full) shown_valid = false;
        return;
    }
    bool band = live();
    bool badge = frame && !band &&
                 badge_on((uint8_t)cfg()->v[LINK_CFG_MODE], (uint8_t)cfg()->v[LINK_CFG_FLAGS],
                          want.link, want.battery_pct);
    if (frame && !band) {
        if (badge != shown_badge) full = true;
        else if (!full && shown_valid) return;
    }
    if (shown_panel_grey != grey_ok() || shown_grey != band_grey()) full = true; /* the band's waveforms change */
    if (!frame && want.status != shown.status) full = true; /* a new status message */
    epd_begin();
    bool ok;
    if (full || !shown_valid) {
        ok = show_full(frame, band, badge);
    } else {
        ok = band_update();
        fast_count++;
    }
    epd_end();
    shown = want;
    shown_badge = badge;
    shown_valid = ok; /* after a timeout the glass is unknown: next one is full */
}

/* Low battery: the fallback layout with "Charge me", one full refresh, then
 * nothing until the charger is connected (design 4.6). */
static void show_charge_me(void)
{
    if (cold()) return; /* tried again at the next battery check */
    want.status = RENDER_ST_CHARGE;
    epd_begin();
    bool ok = show_full(0, false, false);
    epd_end();
    shown = want;
    shown_valid = ok;
    lowbat_shown = true;
}

static void read_sensors(void)
{
    sht40_reading_t r;
    want.sensor_ok = sht40_measure(&r);
    if (want.sensor_ok) {
        /* always Celsius; render converts for display. Offsets from CONFIG
         * (0.01 units) applied here, so panel and HELLO_RSP agree. */
        const settings_t *st = settings_get();
        int32_t t = r.t10 + st->v[LINK_CFG_T_OFF] / 10;
        int32_t rh = (int32_t)r.rh10 + st->v[LINK_CFG_RH_OFF] / 10;
        want.t10 = (int16_t)t;
        want.rh10 = (uint16_t)(rh < 0 ? 0 : rh > 1000 ? 1000 : rh);
    }
}

static void read_clock(void)
{
    rtc_time_t t;
    rtc_get(&t);
    want.hour = t.hour;
    want.min = t.min;
    want.wday = t.wday;
    want.day = t.day;
    want.month = t.month;
}

/* Measured only on battery: a reading under charge reads high. While
 * charging the panel keeps the last on-battery percentage (design 4.6),
 * dotted if there is none yet; HELLO_RSP then reports none (session()). */
static void read_battery(void)
{
    charge_state_t cs = charge_state();
    want.charging = cs == CHG_CHARGING;
    if (cs == CHG_BATTERY) {
        batt_mv = battery_mv();
        if (batt_mv) want.battery_pct = battery_percent(batt_mv);
    } else {
        batt_mv = 0;
        if (cs == CHG_FULL) want.battery_pct = 100;
    }
    (void)lowbat_update(&lowbat, cs == CHG_BATTERY, batt_mv);
    /* Over-discharge (design 4.6): "charge me" is up and the cell reads
     * below stock's dead point twice in a row. On battery this cuts the
     * power, so one sagging or noisy reading must not do it. */
    static uint8_t dead_n;
    dead_n = lowbat_shown && batt_mv && batt_mv < LOWBAT_DEAD_MV ? (uint8_t)(dead_n + 1u) : 0u;
    if (dead_n >= 2u) pin_clr(PIN_PWR_HOLD);
}

/* Settings that change what is drawn. */
static void apply_settings(void)
{
    want.h12 = cfg()->v[LINK_CFG_CLOCK_24H] == 0;
}

/* Dev convenience until TIME arrives over the link (M3): start the RTC from
 * the build timestamp when it was not already running. */
static void set_build_time(void)
{
    static const char months[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
    const char *d = __DATE__, *tm = __TIME__; /* "Oct 31 2026", "20:41:07" */
    rtc_time_t t = {0};
    for (uint8_t m = 0; m < 12; m++)
        if (d[0] == months[3 * m] && d[1] == months[3 * m + 1] && d[2] == months[3 * m + 2])
            t.month = m + 1;
    t.day = (uint8_t)((d[4] == ' ' ? 0 : d[4] - '0') * 10 + d[5] - '0');
    t.year = (uint8_t)((d[9] - '0') * 10 + d[10] - '0');
    t.hour = (uint8_t)((tm[0] - '0') * 10 + tm[1] - '0');
    t.min = (uint8_t)((tm[3] - '0') * 10 + tm[4] - '0');
    t.sec = (uint8_t)((tm[6] - '0') * 10 + tm[7] - '0');
    /* Sakamoto's day of week, 0 = Sunday */
    static const uint8_t k[12] = {0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4};
    uint16_t y = 2000u + t.year - (t.month < 3);
    t.wday = (uint8_t)((y + y / 4 - y / 100 + y / 400 + k[t.month - 1] + t.day) % 7);
    rtc_set(&t);
}

static bool still_pressed(bool (*btn)(void))
{
    delay_ms(30);
    return btn();
}

static uint8_t reset_bits(uint32_t f)
{
    uint8_t b = 0;
    if (f & (RST_POR5V | RST_POR15V)) b |= LINK_RST_POR;
    if (f & RST_LVD) b |= LINK_RST_LVD;
    if (f & RST_WDT) b |= LINK_RST_WDT;
    if (f & RST_RSTB) b |= LINK_RST_PIN;
    if (f & RST_SYSREQ) b |= LINK_RST_SOFT;
    if (f & RST_LOCKUP) b |= LINK_RST_LOCKUP;
    if (f & RST_PCA) b |= LINK_RST_OTHER;
    return b;
}

/* Minutes to the next sync after a session, aligned to the RTC for a
 * success (protocol.md 5.4: "sync interval, aligned to the RTC"). */
static uint32_t due_after(const link_outcome_t *o)
{
    if (o->result == LINK_RES_OK) {
        uint32_t sync = (uint32_t)settings_get()->v[LINK_CFG_SYNC_MIN];
        rtc_time_t t;
        rtc_get(&t);
        uint32_t mod = (t.hour * 60u + t.min) % sync;
        return sync - mod;
    }
    return (o->next_s + 59u) / 60u;
}

/* One link session (protocol.md 5). Returns true if the panel needs a full
 * refresh (a new frame was committed). bench: the MCU's own HOLD 900. */
static bool session(uint8_t trigger, bool bench)
{
    read_sensors(); /* right before the session, never during Wi-Fi */
    charge_state_t cs = charge_state();
    bool on_bat = cs == CHG_BATTERY;
    batt_mv = on_bat ? battery_mv() : 0; /* at rest, PB05 still high */
    uint8_t pct = on_bat && batt_mv ? battery_percent(batt_mv) : LINK_PCT_NONE;
    if (pct != LINK_PCT_NONE) want.battery_pct = pct;
    link_reading_t r = {
        .t_centi = want.sensor_ok ? (int16_t)(want.t10 * 10) : LINK_T_NONE,
        .rh_centi = want.sensor_ok ? (uint16_t)(want.rh10 * 10u) : LINK_RH_NONE,
        .batt_mv = batt_mv,  /* 0 = none: charging or full reads high */
        .batt_pct = pct,
        .charge = cs == CHG_CHARGING ? LINK_CHG_CHARGING : cs == CHG_FULL ? LINK_CHG_FULL
                : LINK_CHG_DISCHARGING,
        .units = want.fahrenheit,
    };
    if (!link_start(clock_ms(), trigger, &r, bench)) {
        /* Low battery: skipped, icon and backoff unchanged (protocol.md
         * 5.2). If this was the timer (or boot) session, the countdown is
         * at 0: schedule the next try as the last result would, or else
         * nothing would ever start a sync again. A button press with a
         * countdown running leaves it alone. */
        if (!link_sched.due_min) {
            static const link_outcome_t ok = { .result = LINK_RES_OK };
            const link_outcome_t *o = link_outcome();
            link_sched.due_min = due_after(o->sessions ? o : &ok);
        }
        return false;
    }
    uint32_t held = 0;
    while (link_active()) {
        wdt_feed();
        uint32_t ev = power_take_events();
        if ((ev & EV_SWITCH) && !power_switch_on())
            link_abort(clock_ms());
        if ((ev & EV_BTN_TOP) && still_pressed(button_top)) {
            link_abort(clock_ms()); /* a press cancels (e.g. a 15 min bench hold) */
            beep(1);
            ev &= ~(uint32_t)EV_BTN_TOP; /* its release is filtered below or by the main loop */
        }
        held |= ev;
        link_poll(clock_ms());
    }
    /* Keep what happened meanwhile, except a top-button press (it would
     * start the next session at once) and feed ticks. */
    power_post_events(held & ~(uint32_t)(EV_BTN_TOP | EV_TICK));

    const link_outcome_t *o = link_outcome();
    want.link = o->icon;
    want.status = o->status;
    if (link_time_valid()) want.time_valid = true;
    read_clock();
    sched_done(&link_sched, due_after(o));
    if (o->result == LINK_RES_OK) nv.boot_fails = 0;
    apply_settings();
    if (o->fw_staged) {
        /* MCU firmware staged and committed (design 4.7): the CBU is off;
         * reset so the bootloader installs it. The new app's boot session
         * fetches a frame. */
        SCB_AIRCR = 0x05FA0004u; /* SYSRESETREQ */
        for (;;) {
        }
    }
    /* Always re-read: an abandoned firmware transfer erased both slot
     * headers and wrote over the slots, so the old pointer is stale. */
    frame = frame_active();
    return o->committed;
}

/* n short 4 kHz beeps on the buzzer (the board has no LED). */
static void beep(uint32_t n)
{
    for (uint32_t i = 0; i < n; i++) {
        for (uint32_t c = 0; c < 320u; c++) { /* 80 ms */
            pin_set(PIN_BUZZER);
            delay_us(125);
            pin_clr(PIN_BUZZER);
            delay_us(125);
        }
        wdt_feed();
        delay_ms(80);
    }
}

typedef enum { G_NONE, G_UNITS, G_SYNC, G_BENCH, G_FACTORY } gesture_t;

/* Button gesture, polled from the first press until both buttons are up
 * (design 4.3). C/F alone: units. Top alone: sync, or held 10 s a bench
 * session. Both held together 10 s: factory reset. Both pressed but let go
 * earlier: nothing. A hold beeps when it reaches its time (bench two,
 * factory three); a sync beeps once on release. can_sync false (low
 * battery): no sync beeps, as the session will not run. Feeds the
 * WDT: its period may be 1.6 s. */
static gesture_t gesture(bool can_sync)
{
    uint32_t top_ms = 0, both_ms = 0;
    bool any_top = false, any_cf = false, any_both = false, factory = false;
    for (;;) {
        bool t = button_top(), c = button_cf();
        if (!t && !c) break;
        any_top |= t;
        any_cf |= c;
        if (t && c) {
            any_both = true;
            if ((both_ms += 10u) >= FACTORY_HOLD_MS && !factory) {
                factory = true;
                beep(3);
            }
        } else {
            both_ms = 0;
            if (t && (top_ms += 10u) == BENCH_HOLD_MS && !any_both && can_sync) beep(2);
        }
        wdt_feed();
        delay_ms(10);
    }
    delay_ms(30); /* release bounce */
    power_post_events(power_take_events() & ~(uint32_t)(EV_BTN_TOP | EV_BTN_CF));
    if (factory) return G_FACTORY;
    if (any_both) return G_NONE;
    if (any_top) {
        if (top_ms >= BENCH_HOLD_MS) return G_BENCH;
        if (can_sync) beep(1);
        return G_SYNC;
    }
    return any_cf ? G_UNITS : G_NONE;
}

/* Both buttons held 10 s (design 4.3): erase both settings sectors and
 * both slot header sectors (the commit words), then FORGET at a session a
 * minute later. Sector erases only (flash.c range check). The panel goes
 * back to the fallback screen, "not set up". */
static void factory_reset(void)
{
    (void)settings_erase();
    (void)flash_erase_sector(FRAME_SLOT_A);
    (void)flash_erase_sector(FRAME_SLOT_B);
    frame_invalidate();
    link_factory_reset();
    nv.boot_fails = 0;
    frame = frame_active(); /* none now */
    want.fahrenheit = settings_get()->units;
    want.status = link_outcome()->status;
    want.link = LINK_WAIT;
    apply_settings();
    link_sched.due_min = 1u;
}

/* Switched off: drop the power hold. On battery that cuts power. On USB the
 * chip stays up, so keep feeding on the feed tick and reset when the switch
 * comes back on (polled each tick). Nothing is redrawn while off. */
static void off_loop(void)
{
    pin_clr(PIN_PWR_HOLD);
    for (;;) {
        wdt_feed();
        (void)power_take_events();
        if (power_switch_on()) SCB_AIRCR = 0x05FA0004u; /* SYSRESETREQ */
        power_sleep();
    }
}

static void switch_off(void)
{
    lowbat_shown = false; /* the off screen replaces "charge me" */
    want.status = RENDER_ST_OFF;
    frame = 0;       /* the off screen is the fallback layout */
    show(true);
    off_loop();
}

int main(void)
{
    board_init();
    wdt_start();
    (void)flash_init(); /* 4 MHz timings; link sessions re-init at 24 MHz */
    uint8_t rst = reset_bits(board_reset_cause);
    link_init(rst);
    if (nv.magic != NV_MAGIC) { /* power-on: RAM content undefined */
        nv.magic = NV_MAGIC;
        nv.boot_fails = 0;
    }

    debug_hold = button_top();
    want.fahrenheit = settings_get()->units;
    want.battery_pct = RENDER_BATT_UNKNOWN;
    apply_settings();

    bool xtl_ok = rtc_init();
    if (!rtc_running_before()) set_build_time();
    want.time_valid = xtl_ok;
    want.status = link_outcome()->status; /* "not set up" until a session ends */
    want.link = LINK_WAIT;

    if (!sht40_probe()) want.sensor_ok = false;
    read_sensors();
    read_clock();
    read_battery();
    last_cs = charge_state();
    power_init();
    if (debug_hold) power_allow_deep(false); /* includes panel BUSY waits */

    delay_ms(2000); /* SWD attach window */

    if (!power_switch_on()) off_loop(); /* booted while off (USB): stock draws nothing */
    frame = frame_active();
    show(true);
    if (LINK_AUTO && (!frame || !rtc_running_before())) {
        uint32_t delay = boot_session_delay_min(rst, nv.boot_fails);
        if (delay) {
            link_sched.due_min = delay;
        } else {
            nv.boot_fails++; /* cleared by a successful session */
            (void)session(LINK_TRIG_BOOT, false);
            show(true);
        }
    } else if (LINK_AUTO) {
        static const link_outcome_t first = { .result = LINK_RES_OK };
        link_sched.due_min = due_after(&first);
    }

    uint32_t minutes = 0;
    for (;;) {
        wdt_feed();
        uint32_t ev = power_take_events();
        bool full = false;

        if ((ev & EV_SWITCH) && !power_switch_on()) switch_off();
        if ((ev & (EV_BTN_CF | EV_BTN_TOP)) &&
            (still_pressed(button_cf) || button_top())) {
            gesture_t g = gesture(!lowbat.low);
            if (g == G_UNITS) {
                want.fahrenheit = !want.fahrenheit; /* band update now */
                sched_units(&link_sched);           /* stored and synced 1-2 min later */
            } else if (g == G_FACTORY) {
                factory_reset();
                lowbat_shown = false;
                full = true;
            } else if ((g == G_SYNC || g == G_BENCH) && !lowbat.low) {
                /* ignores the backoff, not the battery */
                full = session(g == G_BENCH ? LINK_TRIG_BENCH : LINK_TRIG_BUTTON,
                               g == G_BENCH) || full;
            }
        }
        if ((link_bench_req & 0xFFFFFF00u) == LINK_BENCH_REQ) {
            uint8_t trig = (uint8_t)link_bench_req;
            link_bench_req = 0;
            full = session(trig, trig == LINK_TRIG_BENCH) || full;
        }
#ifdef LINK_MBOX
        bench_poll();
#endif
        if (ev & EV_MINUTE) {
            (void)link_minute(); /* DST switch from TIME's next_change */
            rtc_time_t now;
            rtc_get(&now);
            bool draw_tick = clock_tick(now.hour, now.min, (uint8_t)cfg()->v[LINK_CFG_CLOCK_MIN]);
            int16_t old_t = want.t10;
            uint16_t old_rh = want.rh10;
            bool old_ok = want.sensor_ok;
            read_sensors(); /* independent of display cadence and picture mode */
            if (link_time_valid())
                history_sample(link_utc_now(), want.sensor_ok, (int16_t)(want.t10 * 10),
                               (uint16_t)(want.rh10 * 10u));
            if (draw_tick) read_clock();
            else { want.t10 = old_t; want.rh10 = old_rh; want.sensor_ok = old_ok; }
            charge_state_t cs = charge_state(); /* two pin reads */
            want.charging = cs == CHG_CHARGING;
            if (cs != last_cs) {
                last_cs = cs;
                sched_charge(&link_sched); /* design 4.3: session at the next minute */
                if (cs != CHG_BATTERY) read_battery(); /* charger in: clears low battery */
            }
            if (++minutes % 10u == 0) read_battery();

            bool last_ok = link_outcome()->result == LINK_RES_OK;
            uint8_t trig = sched_minute(&link_sched, LINK_AUTO && !lowbat.low, last_ok);
            /* C/F: store the units once the countdown is over (one flash
             * write per burst of presses; nothing if unchanged). */
            if (!link_sched.units_min && want.fahrenheit != settings_get()->units)
                (void)settings_set_units(want.fahrenheit);
            if (trig != SCHED_NONE)
                full = session(trig, false) || full;
            if (DEEPSLEEP_TRIAL_MIN && minutes == DEEPSLEEP_TRIAL_MIN) power_allow_deep(false);
        }

        if (lowbat.low && !lowbat_shown) {
            show_charge_me();
        } else if (!lowbat.low && lowbat_shown) {
            lowbat_shown = false; /* charger connected: back to normal */
            want.status = link_outcome()->status;
            full = true;
        }

        uint32_t full_n = (uint32_t)cfg()->v[LINK_CFG_FULL_N];
        if (full || fast_count >= full_n) show(true);
        else if (!same_screen(&want, &shown) || (!shown_valid && (ev & EV_MINUTE))) show(false);

        if (debug_hold) power_idle();
        else power_sleep();
    }
}
