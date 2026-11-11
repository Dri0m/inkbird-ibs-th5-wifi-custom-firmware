/* Row-streaming screen renderer: no framebuffer.  See render.c for the layout.
 *
 * Panel row x (0..415) is one landscape column: byte k bit 7-b = landscape
 * y = 8*k + b.  Rows may be requested in any order. */
#ifndef RENDER_H
#define RENDER_H

#include <stdbool.h>
#include <stdint.h>

typedef struct {
  int16_t t10;          /* temperature, 0.1 degC                      */
  uint16_t rh10;        /* relative humidity, 0.1 %                   */
  bool fahrenheit;      /* show t10 converted to degF                 */
  bool sensor_ok;       /* false: "--.-"                              */
  bool time_valid;      /* false: "--:--" and no date                 */
  uint8_t hour, min, wday /* 0 = Sun */, day, month /* 1..12 */;
  uint8_t battery_pct;  /* 0..100, RENDER_BATT_UNKNOWN: no on-battery reading */
  uint8_t status;       /* RENDER_ST_*: the fallback screen's message  */
  uint8_t link;         /* band icon: LINK_WAIT, LINK_OK, LINK_ERR     */
  bool charging;        /* bolt in the battery icon                   */
  bool h12;             /* 12 h clock (CONFIG CLOCK_24H = 0): 1..12     */
} screen_t;

#define RENDER_BATT_UNKNOWN 0xFFu /* gauge drawn dotted */

/* Fallback screen messages. 0..2 and 5..7 are link.h's LINK_STATUS_*
 * (the link's verdict); 3 and 4 are main.c's own. */
enum {
  RENDER_ST_NONE,         /* synced, but no frame yet */
  RENDER_ST_NOT_SET_UP,   /* no sync has completed since boot or factory reset */
  RENDER_ST_LINK_ERROR,   /* the Wi-Fi module never answered (no HELLO) */
  RENDER_ST_CHARGE,
  RENDER_ST_OFF,
  RENDER_ST_WIFI,         /* the Wi-Fi module could not join the network */
  RENDER_ST_HA_UNREACH,   /* Wi-Fi up, Home Assistant never connected */
  RENDER_ST_HA_NO_ANSWER, /* HA connected but did not run the sync */
  RENDER_ST_COUNT
};

/* Hour as shown: 0..23, or 1..12 on a 12 h clock. Pure, for host tests. */
uint8_t render_hour(uint8_t hour, bool h12);

enum {
  LINK_WAIT, /* never synced or a sync is due: dotted Wi-Fi arcs   */
  LINK_OK,   /* last sync succeeded (the radio sleeps between)    */
  LINK_ERR,  /* last sync failed: crossed-out Wi-Fi               */
};

/* Band = landscape y 0..55 = bytes 0..6 of panel row x.  White background.
 * Two planes: pixel = hi:lo, 11 white, 10 light grey, 01 dark grey, 00 black
 * (DTM1:DTM2 in the panel's grey mode).  hi alone is the 1 bpp image. */
void render_band_row(const screen_t *s, uint16_t x, uint8_t hi[7], uint8_t lo[7]);

/* Fallback screen (design 4.6): band on top, then a centred status message.
 * Planes as for the band. */
void render_fallback_row(const screen_t *s, uint16_t x, uint8_t hi[30], uint8_t lo[30]);

#endif
