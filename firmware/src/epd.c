#include "epd.h"

#include "board.h"
#include "wdt.h"
#include "power.h"

#define SPI_TXE  (1u << 2)
#define SPI_BUSY (1u << 3)
#define LUT_LEN  56u

/* LUTs as 7-byte groups, zero-padded to 56 bytes when sent. Stock clocks 56
 * bytes from tables that are only 42 long, so 0x21..0x24 pick up the next
 * table's first group at bytes 42..48; E5 showed the controller ignores those
 * bytes, so they are left zero here. Load order is stock's. */
static const uint8_t lut_regs[5] = {0x20, 0x21, 0x24, 0x22, 0x23};

static const uint8_t lut_full[5][7] = {
    {0x01, 0x0F, 0x0F, 0x0F, 0x01, 0x01, 0x01}, /* 0x20 VCOM */
    {0x01, 0x4F, 0x8F, 0x0F, 0x01, 0x01, 0x01}, /* 0x21 WW */
    {0x01, 0x0F, 0x8F, 0x4F, 0x01, 0x01, 0x01}, /* 0x24 BB */
    {0x01, 0x0F, 0x8F, 0x0F, 0x01, 0x01, 0x01}, /* 0x22 */
    {0x01, 0x4F, 0x8F, 0x4F, 0x01, 0x01, 0x01}, /* 0x23 */
};

static const uint8_t lut_fast[5][7] = {
    {0x01, 0x0F, 0x01, 0x00, 0x00, 0x01, 0x01},
    {0x01, 0x0F, 0x01, 0x00, 0x00, 0x01, 0x01},
    {0x01, 0x0F, 0x01, 0x00, 0x00, 0x01, 0x01},
    {0x01, 0x8F, 0x01, 0x00, 0x00, 0x01, 0x01},
    {0x01, 0x4F, 0x01, 0x00, 0x00, 0x01, 0x01},
};

/* 4-grey (tools/epd_bench.py grey_luts, E4): a DC-balanced clear, then a
 * VSH pulse of n frames. Level = 2*p0 + p1: 0x23 light, 0x22 dark. */
#define GREY_LIGHT 4u
#define GREY_DARK  10u
#define GREY_BLACK 30u
static const uint8_t grey_clear[7] = {0x01, 0x4F, 0x8F, 0x4F, 0x8F, 0x01, 0x01};
static const uint8_t lut_grey2[5][7] = {
    {0x01, GREY_BLACK, 0, 0, 0, 1, 1},                                    /* 0x20 VCOM */
    {0x01, GREY_BLACK, 0, 0, 0, 1, 1},                                    /* 0x21 white */
    {0x01, 0x40 | GREY_BLACK, 0, 0, 0, 1, 1},                             /* 0x24 black */
    {0x01, 0x40 | GREY_DARK, GREY_BLACK - GREY_DARK, 0, 0, 1, 1},         /* 0x22 dark */
    {0x01, 0x40 | GREY_LIGHT, GREY_BLACK - GREY_LIGHT, 0, 0, 1, 1},       /* 0x23 light */
};
static const uint8_t vcom_clear[7] = {0x01, 0x0F, 0x0F, 0x0F, 0x0F, 0x01, 0x01};

/* Grey band update in two passes (no flashing clear, unchanged pixels and
 * everything outside the band are code 11 and stay undriven):
 *   ERASE: a changed pixel that was grey or black, coded with its OLD level,
 *          gets a VSL (white) pulse matched to how dark it was;
 *   DRAW:  a changed pixel that becomes grey or black, coded with its NEW
 *          level, gets the VSH pulse that makes that level from white.
 * Erase lengths run a little over the draw lengths so a pixel ends at
 * white; per pixel the DC is close to balanced (W->L->W: +4 -6). Both
 * passes are 16 frames like table B. */
#define BAND_T       16u
#define BAND_BLACK   15u   /* table B's B/W pulse */
#define ERASE_LIGHT  6u
#define ERASE_DARK   12u
#define ERASE_BLACK  15u
#define PULSE(level, n) {0x01, (uint8_t)((level) | (n)), (uint8_t)(BAND_T - (n)), 0, 0, 1, 1}
static const uint8_t lut_erase[5][7] = {
    PULSE(0x00, 0),                                       /* 0x20 VCOM: DC */
    PULSE(0x00, 0),                                       /* 0x21 11: undriven */
    PULSE(0x80, ERASE_BLACK),                             /* 0x24 00: black -> white */
    PULSE(0x80, ERASE_DARK),                              /* 0x22 01: dark -> white */
    PULSE(0x80, ERASE_LIGHT),                             /* 0x23 10: light -> white */
};
static const uint8_t lut_draw[5][7] = {
    PULSE(0x00, 0),
    PULSE(0x00, 0),
    PULSE(0x40, BAND_BLACK),                              /* 0x24 00: white -> black */
    PULSE(0x40, GREY_DARK),                               /* 0x22 01: white -> dark */
    PULSE(0x40, GREY_LIGHT),                              /* 0x23 10: white -> light */
};

static void spi_byte(uint8_t b)
{
    while (!(SPI0_STAT & SPI_TXE)) {
    }
    SPI0_DATA = b;
}

/* As stock (0x4948): after the last write wait for TXE, then for !BUSY, so
 * D/C or CS never changes while a byte is still on the wire. */
static void spi_flush(void)
{
    while (!(SPI0_STAT & SPI_TXE)) {
    }
    while (SPI0_STAT & SPI_BUSY) {
    }
}

static void cmd(uint8_t c)
{
    spi_flush();
    pin_clr(PIN_EPD_DC);
    spi_byte(c);
    spi_flush();
    pin_set(PIN_EPD_DC);
}

static void cmd_data(uint8_t c, const uint8_t *d, uint32_t n)
{
    cmd(c);
    while (n--) spi_byte(*d++);
}

void epd_begin(void)
{
    SYSCTRL_PERI_CLKEN0 |= CLK_SPI0;
    pin_input(PIN_EPD_BUSY, false);
    pin_output(PIN_EPD_RST, true);
    pin_output(PIN_EPD_DC, true);
    pin_af(PIN_EPD_CS, 1, true);
    pin_af(PIN_EPD_SCK, 1, true);
    pin_af(PIN_EPD_MOSI, 1, true);
    SPI0_CR = 0x50;  /* enable, master, mode 0, PCLK/2 */
    SPI0_SSN = 0;    /* CS low for the whole update, as stock */

    pin_set(PIN_EPD_RST);
    delay_ms(1);
    pin_clr(PIN_EPD_RST);
    delay_ms(1);
    pin_set(PIN_EPD_RST);
    delay_ms(10);

    static const uint8_t init[] = {
        0x00, 2, 0x37, 0x01,                   /* PSR */
        0x01, 5, 0x03, 0x10, 0x3F, 0x3F, 0x03, /* PWR */
        0x06, 3, 0x37, 0x3D, 0x3D,             /* BTST */
        0x60, 1, 0x22,                         /* TCON */
        0x82, 1, 0x00,                         /* VDCS */
        0x30, 1, 0x09,                         /* PLL */
        0xE3, 1, 0x55,                         /* PWS */
        0x61, 3, 0xF0, 0x01, 0xA0,             /* TRES 240 x 416 */
    };
    for (uint32_t i = 0; i < sizeof init; i += 2u + init[i + 1])
        cmd_data(init[i], &init[i + 2], init[i + 1]);
}

void epd_plane(uint8_t c, epd_row_fn fn, void *ctx)
{
    uint8_t row[EPD_ROW_BYTES];
    cmd(c);
    for (uint16_t r = 0; r < EPD_ROWS; r++) {
        fn(ctx, r, row);
        for (uint32_t i = 0; i < EPD_ROW_BYTES; i++) spi_byte(row[i]);
        if ((r & 63u) == 63u) wdt_feed();
    }
}

static void send_lut(uint8_t reg, const uint8_t *g0, const uint8_t *g1)
{
    cmd(reg);
    for (uint32_t i = 0; i < LUT_LEN; i++) {
        uint8_t b = 0;
        if (i < 7u) b = g0[i];
        else if (g1 && i < 14u) b = g1[i - 7u];
        spi_byte(b);
    }
}

bool epd_refresh(epd_lut_t lut)
{
    uint8_t cdi = lut == LUT_FULL || lut == LUT_GREY ? 0x97 : 0xD7;
    cmd_data(0x50, &cdi, 1);
    for (uint32_t i = 0; i < 5; i++) {
        if (lut == LUT_FULL) send_lut(lut_regs[i], lut_full[i], 0);
        else if (lut == LUT_FAST) send_lut(lut_regs[i], lut_fast[i], 0);
        else if (lut == LUT_ERASE) send_lut(lut_regs[i], lut_erase[i], 0);
        else if (lut == LUT_DRAW) send_lut(lut_regs[i], lut_draw[i], 0);
        else send_lut(lut_regs[i], i == 0 ? vcom_clear : grey_clear, lut_grey2[i]);
    }
    static const uint8_t autoseq = 0xA5; /* PON -> DRF -> POF */
    cmd_data(0x17, &autoseq, 1);
    spi_flush();
    /* Confirm the controller started (BUSY low) before waiting for idle,
     * so a late BUSY can't make a refresh look finished. */
    for (uint32_t i = 0; i < 20u && pin_read(PIN_EPD_BUSY); i++) delay_ms(1);
    return power_wait_busy(); /* sleeps; the feed tick bounds a missed edge */
}

void epd_end(void)
{
    static const uint8_t check = 0xA5;
    cmd_data(0x07, &check, 1); /* deep sleep; only a HW reset wakes it */
    spi_flush();
    SPI0_SSN = 1;
    SPI0_CR = 0;
    /* In deep sleep the UC8253 wants its control inputs at 0 V against IO
     * leakage (UC8253c datasheet A0.61 p.54); only an RST_N falling edge
     * wakes it, so CS low is harmless (stock holds CS low throughout).
     * RST_N stays high: it has a pull-up, and a low would wake the panel. */
    pin_output(PIN_EPD_SCK, false);
    pin_output(PIN_EPD_MOSI, false);
    pin_output(PIN_EPD_CS, false);
    pin_clr(PIN_EPD_DC);
    SYSCTRL_PERI_CLKEN0 &= ~CLK_SPI0;
}

