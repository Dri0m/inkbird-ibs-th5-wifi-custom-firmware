#include "sht40.h"

uint8_t sht40_crc8(const uint8_t *data, uint8_t len)
{
    uint8_t crc = 0xFF;
    while (len--) {
        crc ^= *data++;
        for (uint8_t i = 0; i < 8; i++)
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
    return crc;
}

int16_t sht40_raw_to_t10(uint16_t raw)
{
    /* T[0.1C] = -450 + 1750*raw/65535, rounded; max 1750*65535 fits in u32. */
    uint32_t v = (1750u * raw + 32767u) / 65535u;
    return (int16_t)((int32_t)v - 450);
}

uint16_t sht40_raw_to_rh10(uint16_t raw)
{
    int32_t v = (int32_t)((1250u * raw + 32767u) / 65535u) - 60;
    if (v < 0) v = 0;
    if (v > 1000) v = 1000;
    return (uint16_t)v;
}

bool sht40_convert(const uint8_t buf[6], sht40_reading_t *r)
{
    if (sht40_crc8(buf, 2) != buf[2] || sht40_crc8(buf + 3, 2) != buf[5])
        return false;
    r->t10 = sht40_raw_to_t10((uint16_t)((buf[0] << 8) | buf[1]));
    r->rh10 = sht40_raw_to_rh10((uint16_t)((buf[3] << 8) | buf[4]));
    return true;
}

#ifndef HOST_TEST
#include "board.h"

/* Bus drive follows stock (0x3584-0x3780): SCL is always a push-pull output;
 * SDA is push-pull while the master sends and switches to input to read ACK /
 * data. Half period 2 us (stock delay 0x7278(2)), 4 us at START/STOP.
 * Difference from stock: SDA's internal pull-up is enabled so a released line
 * reads high even without an external pull-up (verify on the board). */
#define SCL_PORT PORT_A
#define SCL_BIT  11u
#define SDA_PORT PORT_A
#define SDA_BIT  12u
#define HALF_US  2u

#define CMD_MEASURE_HIGH 0xFDu
#define POLL_TRIES       20u   /* x 1 ms; datasheet max 8.3 ms for high precision */

static uint8_t g_addr;         /* 7-bit address, 0 = unknown */

static inline void scl_hi(void) { pin_set(SCL_PORT, SCL_BIT); }
static inline void scl_lo(void) { pin_clr(SCL_PORT, SCL_BIT); }
static inline void sda_drive(bool v)
{
    pin_write(SDA_PORT, SDA_BIT, v);
    GPIO_DIR(SDA_PORT) &= ~(1u << SDA_BIT);       /* output */
}
static inline void sda_release(void) { GPIO_DIR(SDA_PORT) |= 1u << SDA_BIT; }
static inline bool sda_read(void) { return pin_read(SDA_PORT, SDA_BIT); }

static void i2c_stop(void);

/* Bus recovery (I2C spec UM10204 3.1.16): a reset in the middle of a read
 * can leave the sensor driving SDA low, waiting for clocks. With SDA
 * released, clock SCL up to 9 times until SDA reads high, then a STOP. */
static void i2c_recover(void)
{
    sda_release();
    for (uint8_t i = 0; i < 9u && !sda_read(); i++) {
        scl_lo();
        delay_us(HALF_US * 2u);
        scl_hi();
        delay_us(HALF_US * 2u);
    }
    scl_lo(); /* i2c_stop() expects SCL low: SDA low first, then SCL, then SDA */
    delay_us(HALF_US);
    i2c_stop();
}

static void i2c_init(void)
{
    pin_output(SCL_PORT, SCL_BIT, true);
    pin_output(SDA_PORT, SDA_BIT, true);
    GPIO_PU(SDA_PORT) |= 1u << SDA_BIT;
    delay_us(HALF_US);
    i2c_recover();
}

static void i2c_start(void)
{
    sda_drive(true);
    scl_hi();
    delay_us(HALF_US);
    sda_drive(false);
    delay_us(HALF_US);
    scl_lo();
    delay_us(HALF_US);
}

static void i2c_stop(void)
{
    sda_drive(false);
    delay_us(HALF_US);
    scl_hi();
    delay_us(HALF_US);
    sda_drive(true);
    delay_us(HALF_US);
}

/* Send a byte, return true if ACKed. */
static bool i2c_write(uint8_t b)
{
    for (uint8_t i = 0; i < 8; i++) {
        sda_drive((b & 0x80u) != 0);
        b = (uint8_t)(b << 1);
        delay_us(HALF_US);
        scl_hi();
        delay_us(HALF_US);
        scl_lo();
        delay_us(HALF_US);
    }
    sda_release();
    delay_us(HALF_US);
    scl_hi();
    delay_us(HALF_US);
    bool ack = !sda_read();
    scl_lo();
    delay_us(HALF_US);
    sda_drive(false);
    return ack;
}

static uint8_t i2c_read(bool ack)
{
    uint8_t v = 0;
    sda_release();
    for (uint8_t i = 0; i < 8; i++) {
        delay_us(HALF_US);
        scl_hi();
        delay_us(HALF_US);
        v = (uint8_t)((v << 1) | (sda_read() ? 1u : 0u));
        scl_lo();
    }
    sda_drive(!ack);        /* ACK = SDA low */
    delay_us(HALF_US);
    scl_hi();
    delay_us(HALF_US);
    scl_lo();
    delay_us(HALF_US);
    return v;
}

bool sht40_probe(void)
{
    static const uint8_t cand[] = { 0x44, 0x45 };
    i2c_init();
    for (uint8_t i = 0; i < sizeof cand; i++) {
        i2c_start();
        bool ack = i2c_write((uint8_t)(cand[i] << 1));
        i2c_stop();
        if (ack) { g_addr = cand[i]; return true; }
    }
    g_addr = 0;
    return false;
}

bool sht40_measure(sht40_reading_t *r)
{
    if (!g_addr && !sht40_probe())
        return false;

    i2c_start();
    bool ok = i2c_write((uint8_t)(g_addr << 1)) && i2c_write(CMD_MEASURE_HIGH);
    i2c_stop();
    if (!ok)
        return false;

    /* Like stock (0x84F8): poll the read address until the sensor ACKs. */
    bool ready = false;
    for (uint8_t t = 0; t < POLL_TRIES && !ready; t++) {
        delay_ms(1);
        i2c_start();
        ready = i2c_write((uint8_t)((g_addr << 1) | 1u));
        if (!ready) i2c_stop();
    }
    if (!ready)
        return false;

    uint8_t buf[6];
    for (uint8_t i = 0; i < 6; i++)
        buf[i] = i2c_read(i < 5);
    i2c_stop();

    return sht40_convert(buf, r);
}
#endif /* HOST_TEST */
