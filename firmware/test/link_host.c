/* Host harness for the link engine: src/link.c, settings.c and frame.c are
 * compiled with -DLINK_HOST into a shared library together with this file,
 * which fakes flash, RTC and the byte transport. Driven from Python
 * (tools/th5link/tests/test_c_mcu.py) by the same fake CBU and virtual-time
 * loopback that test the reference model.
 *
 *   clang -std=c11 -shared -fPIC -DLINK_HOST -Ifirmware/src firmware/test/link_host.c \
 *     firmware/src/link.c firmware/src/settings.c firmware/src/frame.c -o liblink.dylib
 *
 * Flash model: erase sets a 512 B sector to 0xFF; program can only clear
 * bits (a 0 -> 1 change fails, as on the chip); both refuse anything outside
 * the slots and settings sectors. */
#include <string.h>

#include "flash.h"
#include "frame.h"
#include "link.h"
#include "link_port.h"
#include "rtc.h"
#include "settings.h"

#define EXPORT __attribute__((visibility("default")))

EXPORT uint8_t host_flash[0x20000];
EXPORT uint32_t h_erases, h_programs, h_flash_fail_at;

/* ---- flash ---------------------------------------------------------------- */
bool flash_init(void) { return true; }

static bool in_range(uint32_t a, uint32_t n, uint32_t lo, uint32_t end)
{
    return a >= lo && a < end && n <= end - a;
}

/* As src/flash.c: slots A and B are one range (the firmware staging area,
 * whose IMG_DATA writes cross the A/B boundary). */
bool flash_allowed(uint32_t a, uint32_t n)
{
    return n && (in_range(a, n, FRAME_SLOT_A, FRAME_SLOT_B + FRAME_SLOT_BYTES) ||
                 in_range(a, n, FLASH_SETTINGS_A, FLASH_SETTINGS_B + FLASH_SECTOR_BYTES));
}

bool flash_erase_sector(uint32_t a)
{
    if (a % FLASH_SECTOR_BYTES || !flash_allowed(a, FLASH_SECTOR_BYTES))
        return false;
    if (h_flash_fail_at && a == h_flash_fail_at)
        return false;
    memset(host_flash + a, 0xFF, FLASH_SECTOR_BYTES);
    h_erases++;
    return true;
}

bool flash_program(uint32_t a, const void *src, uint32_t n)
{
    if (!flash_allowed(a, n))
        return false;
    const uint8_t *s = src;
    for (uint32_t i = 0; i < n; i++) {
        if ((host_flash[a + i] & s[i]) != s[i])
            return false;
        host_flash[a + i] = s[i];
    }
    h_programs++;
    return true;
}

/* ---- RTC ------------------------------------------------------------------ */
static rtc_time_t h_rtc;
void rtc_set(const rtc_time_t *t) { h_rtc = *t; }
void rtc_get(rtc_time_t *t) { *t = h_rtc; }
EXPORT void h_rtc_get(uint8_t out[7])
{
    out[0] = h_rtc.year; out[1] = h_rtc.month; out[2] = h_rtc.day; out[3] = h_rtc.wday;
    out[4] = h_rtc.hour; out[5] = h_rtc.min; out[6] = h_rtc.sec;
}

EXPORT void h_rtc_set(const uint8_t in[7])
{
    h_rtc = (rtc_time_t){ .year = in[0], .month = in[1], .day = in[2], .wday = in[3],
                          .hour = in[4], .min = in[5], .sec = in[6] };
}

/* ---- transport ---------------------------------------------------------------- */
static uint8_t s_rx[16384], s_tx[16384];
static uint32_t s_rx_head, s_rx_tail, s_tx_n, s_baud = LINK_BAUD_SLOW;
static bool s_tx_idle = true, s_power;

void lport_init(void) {}
void lport_power(bool on)
{
    s_power = on;
    s_rx_head = s_rx_tail = 0;
    s_tx_idle = true;
    s_baud = LINK_BAUD_SLOW;
    if (!on)
        s_tx_n = 0;
}
int lport_getc(void)
{
    if (s_rx_tail == s_rx_head)
        return -1;
    return s_rx[s_rx_tail++];
}
void lport_write(const uint8_t *p, uint32_t n)
{
    if (!s_power || s_tx_n + n > sizeof s_tx)
        return;
    memcpy(s_tx + s_tx_n, p, n);
    s_tx_n += n;
    s_tx_idle = false;
}
bool lport_tx_idle(void) { return s_tx_idle; }
void lport_set_baud(uint32_t baud) { s_baud = baud; }

/* ---- API for Python -------------------------------------------------------------- */
EXPORT void h_init(uint8_t reset_cause, int erased)
{
    memset(host_flash, erased ? 0xFF : 0x00, sizeof host_flash);
    memset(host_flash + 0x10000, 0xFF, 0x10000);
    if (!erased) /* "used" spare and settings: not erased, no valid record */
        memset(host_flash + FRAME_SLOT_B, 0x5A, FRAME_SLOT_BYTES);
    link_init(reset_cause);
}
EXPORT void h_reload(void) { settings_init(); frame_invalidate(); }
EXPORT void h_load(uint32_t addr, const uint8_t *p, uint32_t n) { memcpy(host_flash + addr, p, n); frame_invalidate(); }
EXPORT int h_start(uint32_t now, uint8_t trig, int bench, uint16_t mv, uint8_t charge)
{
    link_reading_t r = { .t_centi = 2140, .rh_centi = 5280, .batt_mv = mv, .batt_pct = 78,
                         .charge = charge, .units = 0 };
    return link_start(now, trig, &r, bench != 0);
}
EXPORT void h_feed(const uint8_t *p, uint32_t n, uint32_t now)
{
    for (uint32_t i = 0; i < n && s_rx_head < sizeof s_rx; i++)
        s_rx[s_rx_head++] = p[i];
    link_poll(now);
    if (s_rx_tail == s_rx_head)
        s_rx_head = s_rx_tail = 0;
}
EXPORT uint32_t h_take_tx(uint8_t *out, uint32_t max)
{
    uint32_t n = s_tx_n < max ? s_tx_n : max;
    memcpy(out, s_tx, n);
    memmove(s_tx, s_tx + n, s_tx_n - n);
    s_tx_n -= n;
    return n;
}
EXPORT void h_tx_done(uint32_t now) { s_tx_idle = true; link_poll(now); }
EXPORT void h_poll(uint32_t now) { link_poll(now); }
EXPORT uint32_t h_next_deadline(uint32_t now) { return link_next_deadline(now); }
EXPORT int h_active(void) { return link_active(); }
EXPORT uint32_t h_baud(void) { return s_baud; }
EXPORT void h_abort(uint32_t now) { link_abort(now); }
EXPORT int h_minute(void) { return link_minute(); }
EXPORT int h_time_valid(void) { return link_time_valid(); }
EXPORT const link_outcome_t *h_outcome(void) { return link_outcome(); }
EXPORT const link_stats_t *h_stats(void) { return &link_stats; }
EXPORT int32_t h_setting(uint8_t tag) { return settings_get()->v[tag]; }
EXPORT uint32_t h_settings_crc(void) { return settings_crc(settings_get()); }
EXPORT uint32_t h_settings_sector(void) { return settings_sector(); }
EXPORT uint32_t h_settings_seq(void) { return settings_seq(); }
EXPORT uint64_t h_frame_id(void) { return frame_id(frame_active()); }
EXPORT uint32_t h_frame_addr(void) { return (uint32_t)((const uint8_t *)frame_active() - host_flash); }
EXPORT uint32_t h_next_after(uint8_t r, uint32_t sync, uint32_t f, uint32_t n) { return link_next_after(r, sync, f, n); }
EXPORT uint32_t h_days(uint32_t y, uint32_t m, uint32_t d) { return link_days_from_civil(y, m, d); }
EXPORT void h_factory_reset(void) { link_factory_reset(); }
EXPORT uint32_t h_set_units(uint8_t u) { return settings_set_units(u); }
EXPORT uint32_t h_units(void) { return settings_get()->units; }
EXPORT int h_settings_erase(void) { return settings_erase(); }
EXPORT int h_settings_factory(void) { return settings_factory(); }
