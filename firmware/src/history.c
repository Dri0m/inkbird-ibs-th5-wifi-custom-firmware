#include "history.h"
#include "link_proto.h"
#include <string.h>

typedef struct {
    uint32_t hour, id;
    int16_t mean_t, min_t, max_t;
    uint16_t mean_rh, min_rh, max_rh;
    uint8_t count;
} hour_t;

static hour_t records[HISTORY_MAX], current;
static uint8_t count;
static int32_t sum_t;
static uint32_t sum_rh, next_id, last_minute, closed_hour;
static bool active, have_minute, have_closed;

void history_init(void)
{
    memset(records, 0, sizeof records);
    memset(&current, 0, sizeof current);
    count = 0; sum_t = 0; sum_rh = 0; next_id = 0; last_minute = 0; closed_hour = 0;
    active = have_minute = have_closed = false;
}

void history_clock_changed(uint32_t old_utc, uint32_t new_utc, bool had_time)
{
    if (!had_time) return;
    int64_t delta = (int64_t)new_utc - old_utc;
    if (delta < -120 || delta > 120) {
        /* A correction invalidates the unfinished hour, not delivered data.
         * Remember the greatest observed minute to prevent a backwards jump
         * from counting the same interval again. */
        active = false;
    }
}

static void finish(void)
{
    if (!active || !current.count) return;
    current.mean_t = (int16_t)(sum_t / current.count);
    current.mean_rh = (uint16_t)(sum_rh / current.count);
    current.id = ++next_id;
    if (count == HISTORY_MAX) {
        memmove(records, records + 1, (HISTORY_MAX - 1u) * sizeof records[0]);
        count--;
    }
    records[count++] = current;
    closed_hour = current.hour;
    have_closed = true;
    active = false;
}

void history_sample(uint32_t utc, bool valid, int16_t t, uint16_t rh)
{
    uint32_t minute = utc / 60u, hour = utc / 3600u * 3600u;
    if (have_minute && minute <= last_minute) return;
    last_minute = minute; have_minute = true;
    if (active && hour > current.hour) finish();
    if (!valid || (have_closed && hour <= closed_hour)) return;
    if (!active) {
        memset(&current, 0, sizeof current);
        current.hour = hour;
        current.min_t = current.max_t = t;
        current.min_rh = current.max_rh = rh;
        sum_t = 0; sum_rh = 0; active = true;
    }
    if (hour != current.hour || current.count == 255u) return;
    if (t < current.min_t) current.min_t = t;
    if (t > current.max_t) current.max_t = t;
    if (rh < current.min_rh) current.min_rh = rh;
    if (rh > current.max_rh) current.max_rh = rh;
    sum_t += t; sum_rh += rh; current.count++;
}

uint16_t history_snapshot(uint8_t *out, uint32_t *token, uint8_t max_records)
{
    uint8_t sent = count < max_records ? count : max_records;
    out[0] = sent; out[1] = 0;
    *token = sent ? records[sent - 1u].id : 0u;
    for (uint32_t i = 0; i < sent; i++) {
        const hour_t *r = &records[i];
        uint8_t *p = out + 2u + i * HISTORY_RECORD_SIZE;
        link_put32(p, r->hour);
        link_put16(p + 4, (uint16_t)r->mean_t);
        link_put16(p + 6, (uint16_t)r->min_t);
        link_put16(p + 8, (uint16_t)r->max_t);
        link_put16(p + 10, r->mean_rh);
        link_put16(p + 12, r->min_rh);
        link_put16(p + 14, r->max_rh);
        p[16] = r->count;
    }
    return (uint16_t)(2u + sent * HISTORY_RECORD_SIZE);
}

void history_ack(uint32_t token)
{
    uint8_t n = 0;
    while (n < count && records[n].id <= token) n++;
    if (n) {
        count -= n;
        memmove(records, records + n, count * sizeof records[0]);
    }
}
