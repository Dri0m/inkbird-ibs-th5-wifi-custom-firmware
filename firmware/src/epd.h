/* UC8253 240x416 e-paper on SPI0 (evidence/hc32-re-hardware.md, evidence/m0-display-tests.md).
 *
 * The panel is written as 416 gate rows of 30 bytes. Landscape x = row,
 * landscape y = byte * 8 + bit (MSB first), bit 1 = white. After a hardware
 * reset DTM1 (0x10) is the OLD image and DTM2 (0x13) the NEW one, so every
 * update starts with epd_begin() and no LUT swap is needed. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define EPD_ROWS      416
#define EPD_ROW_BYTES 30

enum { EPD_DTM1 = 0x10, EPD_DTM2 = 0x13 };

/* Fills one gate row (landscape column x = row). */
typedef void (*epd_row_fn)(void *ctx, uint16_t row, uint8_t out[EPD_ROW_BYTES]);

typedef enum {
    LUT_FULL,  /* stock table A: flashing 1-bit refresh, ~1.0 s */
    LUT_FAST,  /* stock table B: leaves WW/KK undriven, ~0.41 s; band updates */
    LUT_GREY,  /* 4-grey: light 4, dark 10, black 30 frames, ~1.9 s */
    LUT_ERASE, /* grey band update, pass 1: code 10/01/00 pixels to white, 11 undriven */
    LUT_DRAW,  /* grey band update, pass 2: white pixels to code 10/01/00, 11 undriven */
} epd_lut_t;

void epd_begin(void);                                     /* pins, SPI, HW reset, init */
void epd_plane(uint8_t cmd, epd_row_fn fn, void *ctx);    /* stream one plane */
bool epd_refresh(epd_lut_t lut);                          /* CDI, LUTs, AUTO; false on timeout */
void epd_end(void);                                       /* deep sleep, pins parked */
