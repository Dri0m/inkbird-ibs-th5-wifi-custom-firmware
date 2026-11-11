#include "battery.h"

/* ADC reference: stock selects CR0.REF = 0 (internal 1.5 V) and INREFEN = 1,
 * 12-bit, right aligned.  Full scale 4095 counts = 1500 mV at the pin.
 *
 * Divider ratio (Vbat / Vpin) x 1000, taken from the stock firmware, which
 * works in raw counts only: its "full" clamp, 2833 counts = 1038 mV at the
 * pin, is taken as a charged cell's 4.20 V (4200 / 1038 = 4.047). Stock's
 * "empty" 2252 is then 3.34 V and "dead" 2115 3.13 V. A multimeter reading
 * would only refine the absolute voltage reported to HA. */
#define BAT_DIV_X1000   4047u
#define ADC_VREF_MV10   15000u   /* 1.5 V in 0.1 mV */
#define ADC_FULL        4095u

uint16_t battery_raw_to_mv(uint16_t raw)
{
    uint32_t pin_mv10 = (uint32_t)raw * ADC_VREF_MV10 / ADC_FULL;   /* 0.1 mV */
    return (uint16_t)(pin_mv10 * BAT_DIV_X1000 / 10000u);
}

/* Piecewise-linear resting Li-ion curve (mV -> %), descending. */
static const struct { uint16_t mv; uint8_t pct; } curve[] = {
    { 4200, 100 }, { 4100, 90 }, { 4000, 80 }, { 3920, 70 }, { 3870, 60 },
    { 3830, 50 },  { 3790, 40 }, { 3750, 30 }, { 3700, 20 }, { 3600, 10 },
    { 3300, 0 },
};

uint8_t battery_percent(uint16_t mv)
{
    const uint8_t n = sizeof curve / sizeof curve[0];
    if (mv >= curve[0].mv) return 100;
    if (mv <= curve[n - 1].mv) return 0;
    for (uint8_t i = 1; i < n; i++) {
        if (mv >= curve[i].mv) {
            uint32_t span = (uint32_t)curve[i - 1].mv - curve[i].mv;
            uint32_t dp = (uint32_t)curve[i - 1].pct - curve[i].pct;
            return (uint8_t)(curve[i].pct + ((uint32_t)(mv - curve[i].mv) * dp + span / 2) / span);
        }
    }
    return 0;
}

int16_t c10_to_f10(int16_t c10)
{
    int32_t x = (int32_t)c10 * 9;
    int32_t q = x >= 0 ? (x + 2) / 5 : -((-x + 2) / 5);   /* round half away from 0 */
    return (int16_t)(q + 320);
}

#ifndef HOST_TEST
#include "board.h"

#define CR0_EN        (1u << 0)
#define CR0_CLKDIV(n) ((uint32_t)(n) << 2)
#define CR0_SGLMUX(c) ((uint32_t)(c) << 4)
#define CR0_REF_1V5   (0u << 9)
#define CR0_SAM_12    (3u << 12)     /* 12 ADC clocks sample time */
#define CR0_INREFEN   (1u << 14)
#define BGR_EN        1u
#define ADC_CH_BAT    9u
#define SAMPLES       4u

/* Keep the ADC clock <= ~4 MHz so the 1.5 V reference stays within its
 * 200 ksps limit (datasheet / DDL comment). */
static uint32_t clkdiv_for(uint32_t hz)
{
    if (hz <= 4000000u) return 0;
    if (hz <= 8000000u) return 1;
    if (hz <= 16000000u) return 2;
    return 3;
}

uint16_t battery_mv(void)
{
    uint32_t sum = 0;
    uint8_t got = 0;

    SYSCTRL_PERI_CLKEN0 |= CLK_ADC;
    pin_analog(PIN_BAT_ADC);

    ADC_CR0 = CR0_EN;                    /* stock 0x1FE6: enable first */
    delay_us(20);                        /* stock: 20 us */
    ADC_CR1 = 0;                         /* single mode, right aligned */
    ADC_CR0 = CR0_EN | CR0_CLKDIV(clkdiv_for(SystemCoreClock)) | CR0_REF_1V5 |
              CR0_SAM_12 | CR0_INREFEN | CR0_SGLMUX(ADC_CH_BAT);
    BGR_CR = BGR_EN;
    delay_ms(2);                         /* stock waits 100 us + 2 ms before converting */

    for (uint8_t i = 0; i < SAMPLES + 1u; i++) {
        ADC_ICR = ~1u;                   /* write 0 to clear SGL flag */
        ADC_SGLSTART = 1;
        uint32_t spin = 20000;
        while (!(ADC_IFR & 1u) && --spin) { }
        if (!spin) continue;
        uint16_t v = (uint16_t)(ADC_RESULT & 0xFFFu);
        ADC_ICR = ~1u;
        if (i > 0) { sum += v; got++; }  /* first conversion discarded */
    }

    ADC_SGLSTART = 0;
    ADC_CR0 &= ~CR0_EN;
    BGR_CR = 0;
    SYSCTRL_PERI_CLKEN0 &= ~CLK_ADC;

    if (!got) return 0;
    return battery_raw_to_mv((uint16_t)((sum + got / 2u) / got));
}

charge_state_t charge_state(void)
{
    pin_input(PIN_CHG_STDBY, true);
    pin_input(PIN_CHG_CHRG, true);
    bool stdby = pin_read(PIN_CHG_STDBY);
    bool chrg = pin_read(PIN_CHG_CHRG);
    if (!chrg && stdby) return CHG_CHARGING;
    if (chrg && !stdby) return CHG_FULL;
    return CHG_BATTERY;
}
#endif /* HOST_TEST */
