/* Pure decision logic for main.c (policy.h). */
#include "policy.h"

#include "link_proto.h"
#include "render.h"

bool lowbat_update(lowbat_t *s, bool on_battery, uint16_t mv)
{
    if (!on_battery) {
        s->below = 0;
        s->low = false;
        return false;
    }
    if (mv == 0u)
        return s->low; /* ADC timeout: no evidence either way */
    if (mv < LOWBAT_MV) {
        if (s->below < 255u)
            s->below++;
        if (s->below >= LOWBAT_COUNT)
            s->low = true;
    } else {
        s->below = 0; /* stays latched: a resting cell creeps back up */
    }
    return s->low;
}

uint32_t boot_session_delay_min(uint8_t rst, uint32_t boot_fails)
{
    if (boot_fails >= 2u) {
        uint32_t n = boot_fails - 2u;
        return n >= 3u ? 360u : 60u << n; /* 60, 120, 240, then 6 h */
    }
    if (rst & (LINK_RST_BOR | LINK_RST_LVD | LINK_RST_WDT | LINK_RST_LOCKUP))
        return 30u;
    return 0u;
}

void sched_units(sched_t *s) { s->units_min = UNITS_DELAY_MIN; }

void sched_charge(sched_t *s)
{
    if (s->since_min >= CHARGE_GAP_MIN)
        s->charge_min = 1u;
}

void sched_done(sched_t *s, uint32_t due_min)
{
    s->due_min = due_min;
    s->units_min = 0;
    s->charge_min = 0;
    s->since_min = 0;
}

uint8_t sched_minute(sched_t *s, bool auto_on, bool last_ok)
{
    if (s->since_min < 0xFFFFFFFFu)
        s->since_min++;
    bool units = s->units_min && --s->units_min == 0u;
    bool charge = s->charge_min && --s->charge_min == 0u;
    bool due = s->due_min && --s->due_min == 0u;
    if (!auto_on)
        return SCHED_NONE;
    if (units)
        return LINK_TRIG_UNITS;
    if (charge)
        return LINK_TRIG_CHARGE;
    if (due)
        return last_ok ? LINK_TRIG_TIMER : LINK_TRIG_RETRY;
    return SCHED_NONE;
}

bool clock_tick(uint8_t hour, uint8_t min, uint8_t clock_min)
{
    if (clock_min <= 1u)
        return true;
    uint32_t m = hour * 60u + min;
    while (m >= clock_min) /* <= 1440 / 2 steps; no libgcc division */
        m -= clock_min;
    return m == 0u;
}

bool band_live(uint8_t mode, bool frame_has_band) { return mode == 1u && frame_has_band; }

bool badge_on(uint8_t mode, uint8_t flags, uint8_t link_icon, uint8_t batt_pct)
{
    if (mode != 0u || !(flags & LINK_CFGF_BADGE))
        return false;
    return link_icon == LINK_ERR || (batt_pct != RENDER_BATT_UNKNOWN && batt_pct <= BADGE_BATT_PCT);
}
