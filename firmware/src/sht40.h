/* SHT40 temperature/humidity sensor, bit-banged I2C on PA11 (SCL) / PA12 (SDA). */
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    int16_t t10;     /* 0.1 degC */
    uint16_t rh10;   /* 0.1 %RH, clamped 0..1000 */
} sht40_reading_t;

/* Try 7-bit addresses 0x44 then 0x45; remember the one that ACKed. */
bool sht40_probe(void);
/* High-precision measurement (0xFD), 6 bytes, CRC-8 checked on both words.
 * Returns false on NACK / timeout / CRC failure (r untouched). Probes first
 * if no address is known yet. */
bool sht40_measure(sht40_reading_t *r);

/* Pure helpers (host-testable). */
uint8_t sht40_crc8(const uint8_t *data, uint8_t len);  /* poly 0x31, init 0xFF */
int16_t sht40_raw_to_t10(uint16_t raw);                /* -45 + 175*raw/65535 */
uint16_t sht40_raw_to_rh10(uint16_t raw);              /* -6 + 125*raw/65535, clamped 0..1000 */
/* buf = T_hi T_lo crc RH_hi RH_lo crc. False when either CRC is wrong. */
bool sht40_convert(const uint8_t buf[6], sht40_reading_t *r);
