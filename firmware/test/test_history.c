#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include "history.h"
#include "link_proto.h"

int main(void)
{
    const uint32_t base = 1800000000u / 3600u * 3600u;
    uint8_t out[2 + HISTORY_MAX * HISTORY_RECORD_SIZE];
    uint32_t token, old;
    history_init();
    for (uint32_t i = 0; i < 60; i++) history_sample(base + i * 60, true, (int16_t)(-100 + i * 10), (uint16_t)(5000 + i * 10));
    history_sample(base + 59 * 60, true, 9999, 9999); /* duplicate minute */
    history_sample(base + 3600, true, 100, 4000);
    assert(history_snapshot(out, &old, HISTORY_MAX) == 19 && out[0] == 1);
    assert(link_get32(out + 2) == base && (int16_t)link_get16(out + 6) == 195);
    assert((int16_t)link_get16(out + 8) == -100 && (int16_t)link_get16(out + 10) == 490);
    assert(link_get16(out + 12) == 5295 && out[18] == 60);
    history_sample(base + 7200, true, 200, 3000);
    history_ack(old); /* second completed hour was not in snapshot */
    assert(history_snapshot(out, &token, HISTORY_MAX) == 19 && out[0] == 1 && link_get32(out + 2) == base + 3600);
    history_ack(token);
    assert(history_snapshot(out, &token, HISTORY_MAX) == 2 && out[0] == 0);
    history_clock_changed(base + 7200, base + 18000, true);
    history_sample(base + 18000, true, 300, 2000);
    history_sample(base + 21600, false, 0, 0);
    assert(history_snapshot(out, &token, HISTORY_MAX) == 19 && link_get32(out + 2) == base + 18000);
    history_clock_changed(base + 21600, base + 3600, true);
    history_sample(base + 3600, true, 700, 7000); /* backwards: ignore */
    history_sample(base + 21660, true, 400, 1000);
    history_sample(base + 25200, true, 500, 1000);
    assert(history_snapshot(out, &token, HISTORY_MAX) == 36 && link_get32(out + 19) == base + 21600);
    history_init();
    for (uint32_t i = 0; i < 11; i++) history_sample(base + i * 3600, true, (int16_t)i, 5000);
    assert(history_snapshot(out, &token, HISTORY_MAX) == 138 && out[0] == 8 && link_get32(out + 2) == base + 7200);
    history_ack(token);
    assert(history_snapshot(out, &token, HISTORY_MAX) == 2);
    puts("test_history: all passed");
}
