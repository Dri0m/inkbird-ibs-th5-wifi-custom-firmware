/* MCU side of the MCU <-> CBU link: frame engine, session state machine and
 * request handlers (docs/protocol.md 3-5). The executable reference is
 * tools/th5link/mcumodel.py; this mirrors it.
 *
 * Use (main.c): link_init() once at boot; for a session link_start(), then
 * call link_poll() in a busy loop (feeding the WDT) while link_active().
 * The session runs at HCLK 24 MHz and returns to 4 MHz before link_active()
 * goes false, so the caller may sleep and refresh the panel afterwards.
 * No sleep of any kind while a session is active. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "link_proto.h"

typedef struct {
    int16_t t_centi;     /* 0.01 degC, offsets applied; LINK_T_NONE if none */
    uint16_t rh_centi;   /* 0.01 %RH; LINK_RH_NONE if none */
    uint16_t batt_mv;    /* at rest, before the CBU is powered */
    uint8_t batt_pct;    /* 0..100, LINK_PCT_NONE */
    uint8_t charge;      /* LINK_CHG_* */
    uint8_t units;       /* 0 degC, 1 degF (display setting) */
} link_reading_t;

/* Why the last session failed, shown on the fallback screen. Values match
 * render.h's RENDER_ST_* (3 and 4 are main.c's charge-me and off). */
enum { LINK_STATUS_NONE, LINK_STATUS_NOT_SET_UP, LINK_STATUS_LINK_ERROR,
       LINK_STATUS_WIFI = 5, LINK_STATUS_HA_UNREACH, LINK_STATUS_HA_NO_ANSWER };

typedef struct {
    uint8_t result;      /* LINK_RES_* of the last ended session (0: none yet) */
    uint8_t icon;        /* LINK_WAIT / LINK_OK / LINK_ERR (render.h) */
    uint8_t status;      /* LINK_STATUS_* */
    uint8_t fail_count;
    uint8_t nohello_count;
    bool committed;      /* a frame was committed in the last session */
    bool fw_staged;      /* MCU firmware committed for the bootloader: reset */
    uint32_t next_s;     /* seconds to the next scheduled session */
    uint32_t session_ms; /* CBU-on time of the last session */
    uint32_t sessions;   /* sessions ended since boot */
} link_outcome_t;

#define LINK_BATT_MIN_MV 3650u
/* major << 24 | minor << 16 | patch; also in the app info block (fwu.h) */
#define LINK_FW_VERSION  0x00020005u

/* reset_cause: LINK_RST_* bits for HELLO_RSP. Loads the settings and
 * initialises the transport (the mailbox in bench builds). */
void link_init(uint8_t reset_cause);

/* Power the CBU and open a session. Returns false if it was skipped
 * (battery below 3.65 V and not charging: not a failure, nothing changes).
 * bench: the MCU's own HOLD 900 (protocol.md 4.10). */
bool link_start(uint32_t now_ms, uint8_t trigger, const link_reading_t *r, bool bench);
bool link_active(void);
/* Factory reset done (settings and slots erased by the caller): HELLO_RSP
 * reports FACTORY_RESET and the next session sends FORGET (protocol.md
 * 4.12). */
void link_factory_reset(void);
void link_poll(uint32_t now_ms);
/* PA09 went low: cut the CBU now, result ABORT. */
void link_abort(uint32_t now_ms);
/* Earliest time link_poll() must run again (only meaningful while active
 * and with no RX pending); now + 1000 if nothing is due. */
uint32_t link_next_deadline(uint32_t now_ms);
const link_outcome_t *link_outcome(void);

/* Time from TIME (4.5). */
bool link_time_valid(void);    /* a TIME was applied since boot */
uint32_t link_utc_now(void); /* only valid once link_time_valid() is true */
/* Call once a minute: applies next_offset_min once the RTC passes
 * next_change_utc (DST, protocol.md 4.5). True if the clock was changed. */
bool link_minute(void);

/* Pure helpers, exposed for the host tests. */
uint32_t link_days_from_civil(uint32_t y, uint32_t m, uint32_t d); /* days since 1970-01-01 */
void link_civil_from_days(uint32_t days, uint32_t *y, uint32_t *m, uint32_t *d);
uint32_t link_next_after(uint8_t result, uint32_t sync_min, uint32_t fail_count,
                         uint32_t nohello_count);

/* Counters for the host tests (tools/th5link/tests/test_c_mcu.py);
 * `discarded` also drives the R1b revert (protocol.md 2.3). */
typedef struct {
    uint32_t discarded, dup_requests, reverts;
    uint32_t erases, programs, settings_writes;
} link_stats_t;
extern link_stats_t link_stats;
