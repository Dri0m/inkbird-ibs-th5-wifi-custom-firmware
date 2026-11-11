/* Host test for the pure sensor helpers.
 * clang -std=c11 -Wall -Wextra -DHOST_TEST -I firmware/src firmware/test/test_sensors.c \
 *       firmware/src/sht40.c firmware/src/battery.c -o <out> */
#include <stdio.h>
#include "sht40.h"
#include "battery.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

int main(void)
{
    /* CRC: Sensirion example */
    const uint8_t ex[2] = { 0xBE, 0xEF };
    CHECK(sht40_crc8(ex, 2) == 0x92);

    /* T / RH conversion */
    CHECK(sht40_raw_to_t10(0) == -450);
    CHECK(sht40_raw_to_t10(65535) == 1300);
    CHECK(sht40_raw_to_t10(0x6666) >= 249 && sht40_raw_to_t10(0x6666) <= 261);
    CHECK(sht40_raw_to_rh10(0) == 0);           /* -6 %RH clamped */
    CHECK(sht40_raw_to_rh10(65535) == 1000);    /* 119 %RH clamped */
    uint16_t mid = sht40_raw_to_rh10(0x8000);
    CHECK(mid >= 560 && mid <= 570);
    for (uint32_t r = 1; r < 65536; r++) {
        CHECK(sht40_raw_to_rh10((uint16_t)r) >= sht40_raw_to_rh10((uint16_t)(r - 1)));
        CHECK(sht40_raw_to_rh10((uint16_t)r) <= 1000);
    }

    /* convert with CRC */
    uint8_t b[6] = { 0x66, 0x66, 0, 0x80, 0x00, 0 };
    b[2] = sht40_crc8(b, 2);
    b[5] = sht40_crc8(b + 3, 2);
    sht40_reading_t rd;
    CHECK(sht40_convert(b, &rd));
    CHECK(rd.t10 >= 249 && rd.t10 <= 261);
    b[5] ^= 1;
    CHECK(!sht40_convert(b, &rd));
    b[5] ^= 1; b[2] ^= 1;
    CHECK(!sht40_convert(b, &rd));

    /* Fahrenheit */
    CHECK(c10_to_f10(0) == 320);
    CHECK(c10_to_f10(100) == 500);
    CHECK(c10_to_f10(-400) == -400);
    CHECK(c10_to_f10(-100) == 140);
    CHECK(c10_to_f10(-1) == 318);      /* -1.8 -> -2 */
    CHECK(c10_to_f10(1) == 322);
    CHECK(c10_to_f10(-450) == -490);
    CHECK(c10_to_f10(-3) == 315);      /* -5.4 -> -5 */
    CHECK(c10_to_f10(-4) == 313);      /* -7.2 -> -7 */
    CHECK(c10_to_f10(3) == 325);

    /* battery percent monotonic, bounds */
    CHECK(battery_percent(0) == 0);
    CHECK(battery_percent(3300) == 0);
    CHECK(battery_percent(4200) == 100);
    CHECK(battery_percent(5000) == 100);
    uint8_t prev = 0;
    for (uint16_t mv = 0; mv <= 4400; mv++) {
        uint8_t p = battery_percent(mv);
        CHECK(p >= prev && p <= 100);
        prev = p;
    }
    CHECK(battery_raw_to_mv(2833) >= 4190 && battery_raw_to_mv(2833) <= 4210);
    CHECK(battery_raw_to_mv(0) == 0);

    printf(fails ? "%d FAILURES\n" : "all tests passed\n", fails);
    return fails != 0;
}
