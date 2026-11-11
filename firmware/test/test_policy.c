/* Host test of src/policy.c (main.c's decisions) and render_hour.
 *   make test-policy */
#include <stdio.h>

#include "link_proto.h"
#include "policy.h"
#include "render.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

int main(void)
{
    /* ---- low battery: 3 consecutive on-battery readings below 3.40 V ---- */
    lowbat_t lb = {0};
    CHECK(!lowbat_update(&lb, true, 3390));
    CHECK(!lowbat_update(&lb, true, 3390));
    CHECK(!lowbat_update(&lb, true, 3500));          /* a good reading resets the run */
    CHECK(!lowbat_update(&lb, true, 3300));
    CHECK(!lowbat_update(&lb, true, 0));             /* no reading: no change */
    CHECK(!lowbat_update(&lb, true, 3300));
    CHECK(lowbat_update(&lb, true, 3399));           /* third in a row */
    CHECK(lowbat_update(&lb, true, 3600));           /* latched while on battery */
    CHECK(!lowbat_update(&lb, false, 3300));         /* charger connected: cleared */
    CHECK(lb.below == 0);
    CHECK(!lowbat_update(&lb, false, 3000));         /* charging readings never count */
    CHECK(!lowbat_update(&lb, false, 3000));
    CHECK(!lowbat_update(&lb, false, 3000));

    /* ---- boot session gate ---- */
    CHECK(boot_session_delay_min(LINK_RST_POR, 0) == 0);
    CHECK(boot_session_delay_min(LINK_RST_SOFT, 1) == 0);
    CHECK(boot_session_delay_min(LINK_RST_PIN, 0) == 0);
    CHECK(boot_session_delay_min(LINK_RST_WDT, 0) == 30);
    CHECK(boot_session_delay_min(LINK_RST_LVD | LINK_RST_POR, 0) == 30);
    CHECK(boot_session_delay_min(LINK_RST_LOCKUP, 1) == 30);
    CHECK(boot_session_delay_min(LINK_RST_BOR, 0) == 30);
    CHECK(boot_session_delay_min(LINK_RST_SOFT, 2) == 60);
    CHECK(boot_session_delay_min(LINK_RST_SOFT, 3) == 120);
    CHECK(boot_session_delay_min(LINK_RST_WDT, 4) == 240);
    CHECK(boot_session_delay_min(LINK_RST_POR, 5) == 360);
    CHECK(boot_session_delay_min(LINK_RST_POR, 1000) == 360);

    /* ---- scheduler ---- */
    sched_t s = {0};
    sched_done(&s, 3);
    CHECK(sched_minute(&s, true, true) == SCHED_NONE);
    CHECK(sched_minute(&s, true, true) == SCHED_NONE);
    CHECK(sched_minute(&s, true, true) == LINK_TRIG_TIMER);
    CHECK(sched_minute(&s, true, true) == SCHED_NONE);  /* countdown spent */
    sched_done(&s, 1);
    CHECK(sched_minute(&s, true, false) == LINK_TRIG_RETRY);

    /* C/F: the 2nd tick, restarted by another press, ignores the sync countdown */
    sched_done(&s, 60);
    sched_units(&s);
    CHECK(sched_minute(&s, true, true) == SCHED_NONE);
    sched_units(&s);                                   /* pressed again */
    CHECK(sched_minute(&s, true, true) == SCHED_NONE);
    CHECK(sched_minute(&s, true, true) == LINK_TRIG_UNITS);
    sched_done(&s, 60);
    CHECK(s.units_min == 0 && s.since_min == 0);

    /* CHARGE: next tick, but not within 15 min of the last session */
    sched_charge(&s);
    CHECK(s.charge_min == 0);
    for (int i = 0; i < 15; i++) CHECK(sched_minute(&s, true, true) == SCHED_NONE);
    sched_charge(&s);
    CHECK(sched_minute(&s, true, true) == LINK_TRIG_CHARGE);

    /* UNITS wins over a sync due in the same tick; both served by one session */
    sched_done(&s, 2);
    sched_units(&s);
    CHECK(sched_minute(&s, true, true) == SCHED_NONE);
    CHECK(sched_minute(&s, true, true) == LINK_TRIG_UNITS);
    sched_done(&s, 60);
    CHECK(s.due_min == 60);

    /* automatic sessions off (bench build, low battery): nothing fires, the
     * countdowns still run out */
    sched_done(&s, 1);
    sched_units(&s);
    CHECK(sched_minute(&s, false, true) == SCHED_NONE);
    CHECK(sched_minute(&s, false, true) == SCHED_NONE);
    CHECK(s.due_min == 0 && s.units_min == 0);

    /* ---- clock period, band, badge ---- */
    CHECK(clock_tick(13, 37, 1));
    CHECK(clock_tick(13, 37, 0));
    CHECK(clock_tick(0, 0, 30) && clock_tick(13, 30, 30) && !clock_tick(13, 37, 30));
    CHECK(clock_tick(13, 35, 5) && !clock_tick(13, 36, 5));
    CHECK(clock_tick(1, 1, 7) == ((61 % 7) == 0));
    CHECK(clock_tick(23, 59, 29) == (((23 * 60 + 59) % 29) == 0));
    CHECK(band_live(1, true) && !band_live(1, false) && !band_live(0, true));
    CHECK(!badge_on(1, LINK_CFGF_BADGE, LINK_ERR, 5));          /* forecast: no badge */
    CHECK(!badge_on(0, 0, LINK_ERR, 5));                        /* flag off */
    CHECK(badge_on(0, LINK_CFGF_BADGE, LINK_ERR, 80));
    CHECK(badge_on(0, LINK_CFGF_BADGE, LINK_OK, 10));
    CHECK(!badge_on(0, LINK_CFGF_BADGE, LINK_OK, 11));
    CHECK(!badge_on(0, LINK_CFGF_BADGE, LINK_OK, RENDER_BATT_UNKNOWN));

    /* ---- 12 h clock ---- */
    CHECK(render_hour(0, false) == 0 && render_hour(13, false) == 13);
    CHECK(render_hour(0, true) == 12 && render_hour(12, true) == 12);
    CHECK(render_hour(1, true) == 1 && render_hour(13, true) == 1 && render_hour(23, true) == 11);

    printf(fails ? "test_policy: %d FAILED\n" : "test_policy: all passed\n", fails);
    return fails != 0;
}
