/* Battery voltage (ADC AIN9 / PB01), Li-ion percent, TC4056A charge state. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef enum { CHG_BATTERY, CHG_CHARGING, CHG_FULL } charge_state_t;

/* Battery voltage in mV. Turns on ADC clock + BGR, averages 4 conversions,
 * then gates everything off again. Returns 0 on ADC timeout. */
uint16_t battery_mv(void);

/* Open-circuit Li-ion curve, linear between points (see battery.c). 0..100. */
uint8_t battery_percent(uint16_t mv);

/* PF00 = STDBY, PF01 = CHRG (both active low, TC4056A open-drain). */
charge_state_t charge_state(void);

/* Celsius tenths -> Fahrenheit tenths, rounded half away from zero. */
int16_t c10_to_f10(int16_t c10);

/* Pure helper: raw 12-bit ADC counts -> battery mV using the divider define. */
uint16_t battery_raw_to_mv(uint16_t raw);
