/* MCU side of the link (docs/protocol.md 3-5). Mirrors the reference model
 * tools/th5link/mcumodel.py (session state machine, handlers) and link.py
 * (stop-and-wait responder, duplicates, baud probe and revert rules).
 *
 * Transport: link_port.h (UART0 or the SWD mailbox). Flash: flash.c, with the
 * timings for 24 MHz written at session start (notes-link.md, clock choice).
 * Time is in ms from the caller (clock_ms() on the target); every comparison
 * is wrap-safe. */
#include "link.h"

#include <stddef.h>
#include <string.h>

#include "flash.h"
#include "frame.h"
#include "fwu.h"
#include "link_port.h"
#include "render.h"
#include "rtc.h"
#include "history.h"
#include "settings.h"

#ifndef LINK_HOST
#include "board.h"
#include "wdt.h"
#endif

link_stats_t link_stats;

/* ---- hardware glue (stubbed in the host tests) ------------------------ */
#ifdef LINK_HOST
static void hw_session(bool on) { (void)on; (void)flash_init(); }
static void feed(void) {}
#else
/* The whole session runs at 24 MHz (UART baud dividers, protocol.md 2.1);
 * flash timings follow the clock. Back to 4 MHz before anything else. */
static void hw_session(bool on)
{
    (void)clock_set_fast(on);
    (void)flash_init();
}
static void feed(void) { wdt_feed(); }
#endif

enum { LINK_ST_OFF, LINK_ST_WAIT_HELLO, LINK_ST_HELLO_SENT, LINK_ST_PROBE, LINK_ST_LINKED,
       LINK_ST_GRACE, LINK_ST_CLOSING };

#define AFTER_NONE   0u
#define AFTER_FAST   1u   /* HELLO_RSP at 500 k left: switch, open the probe */
#define AFTER_LINKED 2u   /* HELLO_RSP at 115200 left */
#define AFTER_BYE    3u   /* BYE ACK left: PB05 high in 50 ms */

#define TUYA_WINDOW  71u  /* longest Tuya frame looked for: 64 data bytes */
#define DUP_MAX      64u  /* largest stored response (HELLO_RSP 52 B) */
#define IMG_MAX      (FRAME_SLOT_BYTES - FRAME_HDR_BYTES)

static bool due(uint32_t now, uint32_t t) { return (int32_t)(now - t) >= 0; }

static struct {
    /* session */
    uint8_t state;
    bool cbu_on, holding, bench, factory;
    bool hold_seen;  /* the CBU was granted a HOLD this session (maintenance, OTA) */
    bool got_hello;  /* a HELLO arrived this session */
    uint8_t trigger, result_pending, reset_cause;
    uint32_t t_on, deadline, hello_deadline, probe_deadline;
    bool off_pending;
    uint32_t off_at;
    uint32_t closing_at;  /* BYE ACK queued: CLOSING ends by closing_at + 100 ms */
    bool baud_pending;    /* L.baud not yet applied to the port (TX busy) */
    link_reading_t rd;
    /* line */
    uint32_t baud;
    bool sticky_slow;
    uint32_t fast_since;
    bool have_valid;
    uint32_t last_valid;
    /* receiver (3.4) */
    uint8_t rx[LINK_RX_BUF];
    uint32_t rx_n, last_byte;
    uint8_t tuya[TUYA_WINDOW];
    uint32_t tuya_n;
    /* responder (3.5) */
    uint8_t tx[LINK_TX_BUF];
    uint16_t r_len;
    uint8_t r_after, after;
    bool dup_valid;
    uint8_t dup_seq, dup_cmd, dup_len;
    uint16_t dup_crc;
    uint8_t dup_buf[DUP_MAX];
    /* the MCU's own request (FORGET) */
    bool out_active, out_deadline_set, forget_pending, forget_sent;
    uint8_t out_seq, out_cmd, out_attempts, tx_seq;
    uint32_t out_deadline;
    uint8_t out_raw[LINK_OVERHEAD];
    /* image transfer (4.7-4.9) */
    bool img_open, img_fw, fw_done;
    uint32_t img_slot, img_len, img_crc, img_next, img_erased_end;
    uint64_t img_id;
    /* time (4.5) */
    bool time_valid;
    int16_t offset, next_offset;
    uint32_t next_utc;
    /* history for HELLO_RSP */
    uint32_t history_token;
    uint32_t last_session_ms;
    uint8_t last_result;
    uint8_t net_state;
} L;

static link_outcome_t s_out = { .icon = LINK_WAIT, .status = LINK_STATUS_NOT_SET_UP };

/* A failed session after HELLO: how far the CBU got (its last NET state),
 * for the fallback screen's message. */
static uint8_t net_status(uint8_t result)
{
    if (result == LINK_RES_LINK)
        return LINK_STATUS_LINK_ERROR;
    switch (L.net_state) {
    case 0:
    case LINK_NET_WIFI_CONNECTING:
    case LINK_NET_FAIL_WIFI:
    case LINK_NET_FAIL_NO_CREDENTIALS:
        return LINK_STATUS_WIFI;
    case LINK_NET_WIFI_UP:
    case LINK_NET_FAIL_API:
        return LINK_STATUS_HA_UNREACH;
    default: /* API_UP, HA_CONFIG, OTA, FAIL_NO_HA: HA was there */
        return LINK_STATUS_HA_NO_ANSWER;
    }
}

/* ---- calendar --------------------------------------------------------- */

uint32_t link_days_from_civil(uint32_t y, uint32_t m, uint32_t d)
{
    y -= m <= 2u;
    uint32_t era = y / 400u, yoe = y - era * 400u;
    uint32_t doy = (153u * (m > 2u ? m - 3u : m + 9u) + 2u) / 5u + d - 1u;
    uint32_t doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
    return era * 146097u + doe - 719468u;
}

void link_civil_from_days(uint32_t days, uint32_t *y, uint32_t *m, uint32_t *d)
{
    uint32_t z = days + 719468u, era = z / 146097u, doe = z - era * 146097u;
    uint32_t yoe = (doe - doe / 1460u + doe / 36524u - doe / 146096u) / 365u;
    uint32_t doy = doe - (365u * yoe + yoe / 4u - yoe / 100u);
    uint32_t mp = (5u * doy + 2u) / 153u;
    *d = doy - (153u * mp + 2u) / 5u + 1u;
    *m = mp < 10u ? mp + 3u : mp - 9u;
    *y = yoe + era * 400u + (*m <= 2u);
}

static void rtc_set_local(uint32_t local)
{
    uint32_t days = local / 86400u, sod = local % 86400u, y, m, d;
    link_civil_from_days(days, &y, &m, &d);
    rtc_time_t t = {
        .year = (uint8_t)(y - 2000u), .month = (uint8_t)m, .day = (uint8_t)d,
        .wday = (uint8_t)((days + 4u) % 7u), /* 1970-01-01 was a Thursday */
        .hour = (uint8_t)(sod / 3600u), .min = (uint8_t)(sod / 60u % 60u), .sec = (uint8_t)(sod % 60u),
    };
    rtc_set(&t);
}

static uint32_t rtc_local_now(void)
{
    rtc_time_t t;
    rtc_get(&t);
    return link_days_from_civil(2000u + t.year, t.month, t.day) * 86400u +
           t.hour * 3600u + t.min * 60u + t.sec;
}

bool link_time_valid(void) { return L.time_valid; }

uint32_t link_utc_now(void)
{
    return rtc_local_now() - (uint32_t)(int32_t)L.offset * 60u;
}

bool link_minute(void)
{
    if (!L.time_valid || !L.next_utc)
        return false;
    uint32_t utc = rtc_local_now() - (uint32_t)(int32_t)L.offset * 60u;
    if ((int32_t)(utc - L.next_utc) < 0)
        return false;
    rtc_set_local(utc + (uint32_t)(int32_t)L.next_offset * 60u);
    L.offset = L.next_offset;
    L.next_utc = 0;
    return true;
}

/* ---- schedule (5.4) ----------------------------------------------------- */

uint32_t link_next_after(uint8_t result, uint32_t sync_min, uint32_t fail_count,
                         uint32_t nohello_count)
{
    uint32_t interval = sync_min * 60u;
    switch (result) {
    case LINK_RES_NO_HELLO:
    case LINK_RES_TUYA:
        return nohello_count == 1u ? 60u : nohello_count == 2u ? 300u : 24u * 3600u;
    case LINK_RES_NET_FAIL:
    case LINK_RES_CAP:
    case LINK_RES_LINK:
        for (uint32_t i = 0; i < fail_count && interval < 6u * 3600u; i++)
            interval *= 2u;
        return interval < 6u * 3600u ? interval : 6u * 3600u;
    default: /* OK; ABORT keeps the schedule */
        return interval;
    }
}

/* ---- line: baud, receiver ------------------------------------------------ */

static void rx_drop(uint32_t n)
{
    memmove(L.rx, L.rx + n, L.rx_n - n);
    L.rx_n -= n;
    link_stats.discarded += n;
}

/* The port changes rate only once the last byte has left the shifter
 * (protocol.md 2.3), so a revert decided while a response is still going
 * out waits for TC. */
static void apply_baud(void)
{
    if (L.baud_pending && lport_tx_idle()) {
        L.baud_pending = false;
        lport_set_baud(L.baud);
    }
}

static void set_baud(uint32_t baud, uint32_t now)
{
    if (baud == L.baud)
        return;
    L.baud = baud;
    L.baud_pending = true;
    apply_baud();
    link_stats.discarded += L.rx_n; /* parser reset */
    L.rx_n = 0;
    L.fast_since = now;
}

/* R1: down to 115200 for the rest of this power-on (protocol.md 2.3). */
static void revert(uint32_t now)
{
    if (L.baud != LINK_BAUD_SLOW)
        link_stats.reverts++;
    L.sticky_slow = true;
    set_baud(LINK_BAUD_SLOW, now);
}

/* A complete, checksum-valid Tuya MCU frame ending with the byte just added
 * means a stock CBU (D17). Every such frame is <= TUYA_WINDOW bytes. */
static bool tuya_push(uint8_t c)
{
    if (L.tuya_n == TUYA_WINDOW) {
        memmove(L.tuya, L.tuya + 1, TUYA_WINDOW - 1u);
        L.tuya_n--;
    }
    L.tuya[L.tuya_n++] = c;
    const uint8_t *w = L.tuya;
    for (uint32_t i = 0; i + 7u <= L.tuya_n; i++) {
        if (w[i] != 0x55u || w[i + 1] != 0xAAu || (w[i + 2] != 0u && w[i + 2] != 3u))
            continue;
        uint32_t n = (uint32_t)w[i + 4] << 8 | w[i + 5];
        if (n > 64u || i + 7u + n != L.tuya_n)
            continue;
        uint32_t sum = 0;
        for (uint32_t k = i; k < i + 6u + n; k++)
            sum += w[k];
        if ((sum & 0xFFu) == w[i + 6u + n])
            return true;
    }
    return false;
}

/* ---- responses ---------------------------------------------------------- */

#define RSP (L.tx + LINK_HDR_BYTES)

static uint8_t ack(uint8_t cmd, uint16_t extra)
{
    RSP[LINK_ACK_O_CMD] = cmd;
    L.r_len = (uint16_t)(1u + extra);
    return LINK_CMD_ACK;
}

static uint8_t err(uint8_t cmd, uint8_t code, uint32_t detail)
{
    RSP[LINK_ERR_O_CMD] = cmd;
    RSP[LINK_ERR_O_CODE] = code;
    link_put32(RSP + LINK_ERR_O_DETAIL, detail);
    L.r_len = LINK_ERR_LEN;
    return LINK_CMD_ERR;
}

/* ---- request handlers (4) --------------------------------------------------- */

static uint8_t boot_ver(void);

static uint8_t h_hello(const uint8_t *p)
{
    if (p[LINK_HELLO_O_PROTO_MAX] < 1u)
        return err(LINK_CMD_HELLO, LINK_E_VERSION, LINK_VER);
    uint16_t rx_max = link_get16(p + LINK_HELLO_O_RX_MAX);
    if (rx_max < LINK_HELLO_RSP_LEN)
        return err(LINK_CMD_HELLO, LINK_E_BAD_VALUE, LINK_HELLO_O_RX_MAX);
    L.img_open = false; /* a (re)booted CBU: no resume */
    L.got_hello = true;
    bool slow_only = p[LINK_HELLO_O_FLAGS] & LINK_HELLO_F_SLOW_ONLY;
    uint32_t baud = L.sticky_slow || slow_only ? LINK_BAUD_SLOW : LINK_BAUD_FAST;
    L.state = LINK_ST_HELLO_SENT;

    const frame_hdr_t *act = frame_active();
    const link_reading_t *r = &L.rd;
    uint16_t flags = (uint16_t)((L.time_valid ? LINK_F_TIME_VALID : 0) |
                                (act ? LINK_F_FRAME_VALID : 0) |
                                (r->t_centi != LINK_T_NONE ? LINK_F_SENSOR_OK : 0) |
                                (L.forget_pending ? LINK_F_FORGET_PENDING : 0) |
                                (L.bench ? LINK_F_BENCH : 0) |
                                (L.factory ? LINK_F_FACTORY_RESET : 0));
    uint8_t *o = RSP;
    memset(o, 0, LINK_HELLO_RSP_LEN);
    o[LINK_HR_O_PROTO] = LINK_VER;
    o[LINK_HR_O_RESET_CAUSE] = L.reset_cause;
    link_put32(o + LINK_HR_O_BAUD, baud);
    link_put16(o + LINK_HR_O_RX_MAX, LINK_MAX_TO_MCU);
    link_put32(o + LINK_HR_O_FW, LINK_FW_VERSION);
    link_put64(o + LINK_HR_O_FRAME_ID, frame_id(act));
    link_put32(o + LINK_HR_O_SETTINGS_CRC, settings_crc(settings_get()));
    o[LINK_HR_O_UNITS] = r->units;
    o[LINK_HR_O_TRIGGER] = L.trigger;
    link_put16(o + LINK_HR_O_FLAGS, flags);
    link_put16(o + LINK_HR_O_T, (uint16_t)r->t_centi);
    link_put16(o + LINK_HR_O_RH, r->rh_centi);
    link_put16(o + LINK_HR_O_BATT_MV, r->batt_mv);
    o[LINK_HR_O_BATT_PCT] = r->batt_pct;
    o[LINK_HR_O_CHARGE] = r->charge;
    o[LINK_HR_O_BOOT_VER] = boot_ver();
    link_put32(o + LINK_HR_O_LAST_MS, L.last_session_ms);
    o[LINK_HR_O_LAST_RESULT] = L.last_result;
    o[LINK_HR_O_FAIL_COUNT] = s_out.fail_count;
    L.r_len = LINK_HELLO_RSP_LEN;
    L.history_token = 0;
    if (rx_max >= LINK_HELLO_RSP_LEN + 2u) {
        uint32_t room = (rx_max - LINK_HELLO_RSP_LEN - 2u) / HISTORY_RECORD_SIZE;
        if (room > HISTORY_MAX) room = HISTORY_MAX;
        L.r_len += history_snapshot(o + LINK_HELLO_RSP_LEN, &L.history_token, (uint8_t)room);
    }
    L.r_after = baud == LINK_BAUD_FAST ? AFTER_FAST : AFTER_LINKED;
    return LINK_CMD_HELLO_RSP;
}

static uint8_t h_net(const uint8_t *p, uint32_t now)
{
    L.net_state = p[LINK_NET_O_STATE];
    if ((L.net_state & LINK_NET_FAILED) && L.state != LINK_ST_GRACE) {
        L.state = LINK_ST_GRACE;
        L.result_pending = LINK_RES_NET_FAIL;
        if (due(L.deadline, now + LINK_T_NET_GRACE_MS))
            L.deadline = now + LINK_T_NET_GRACE_MS;
    }
    return ack(LINK_CMD_NET, 0);
}

static int32_t s16(const uint8_t *p) { return (int16_t)link_get16(p); }

static uint8_t h_time(const uint8_t *p)
{
    uint32_t utc = link_get32(p + LINK_TIME_O_UTC), next = link_get32(p + LINK_TIME_O_NEXT_UTC);
    int32_t off = s16(p + LINK_TIME_O_OFFSET), noff = s16(p + LINK_TIME_O_NEXT_OFFSET);
    if (utc < LINK_TIME_UTC_MIN)
        return err(LINK_CMD_TIME, LINK_E_BAD_VALUE, LINK_TIME_O_UTC);
    if (off > LINK_TIME_OFFSET_MAX || off < -LINK_TIME_OFFSET_MAX)
        return err(LINK_CMD_TIME, LINK_E_BAD_VALUE, LINK_TIME_O_OFFSET);
    /* The RTC holds a 2-digit BCD year: local time must stay before 2100. */
    if ((uint64_t)utc + (uint64_t)(int64_t)(off * 60) >= LINK_TIME_UTC_MAX)
        return err(LINK_CMD_TIME, LINK_E_BAD_VALUE, LINK_TIME_O_UTC);
    if (next >= LINK_TIME_UTC_MAX)
        return err(LINK_CMD_TIME, LINK_E_BAD_VALUE, LINK_TIME_O_NEXT_UTC);
    if (next && next <= utc)
        return err(LINK_CMD_TIME, LINK_E_BAD_VALUE, LINK_TIME_O_NEXT_UTC);
    if (noff > LINK_TIME_OFFSET_MAX || noff < -LINK_TIME_OFFSET_MAX)
        return err(LINK_CMD_TIME, LINK_E_BAD_VALUE, LINK_TIME_O_NEXT_OFFSET);
    history_clock_changed(L.time_valid ? link_utc_now() : 0, utc, L.time_valid);
    rtc_set_local(utc + (uint32_t)(off * 60));
    L.time_valid = true;
    L.offset = (int16_t)off;
    L.next_utc = next;
    L.next_offset = (int16_t)noff;
    return ack(LINK_CMD_TIME, 0);
}

static uint8_t h_config(const uint8_t *p, uint32_t n)
{
    settings_t ns = *settings_get();
    uint32_t present = 0;
    for (uint32_t i = 0; i < n;) {
        if (i + 2u > n || i + 2u + p[i + 1] > n)
            return err(LINK_CMD_CONFIG, LINK_E_BAD_LEN, i);
        uint8_t tag = p[i], len = p[i + 1], want = settings_tag_len(tag);
        if (want) {
            if (len != want)
                return err(LINK_CMD_CONFIG, LINK_E_BAD_LEN, i);
            ns.v[tag] = len == 1u ? (int32_t)p[i + 2]
                      : (tag == LINK_CFG_T_OFF || tag == LINK_CFG_RH_OFF) ? s16(p + i + 2)
                      : (int32_t)link_get16(p + i + 2);
            present |= 1u << tag;
        }
        i += 2u + len; /* unknown tags are skipped */
    }
    for (uint8_t tag = 1; tag <= LINK_CFG_TAG_COUNT; tag++)
        if ((present >> tag & 1u) && !settings_tag_ok(tag, ns.v[tag]))
            return err(LINK_CMD_CONFIG, LINK_E_BAD_VALUE, tag);
    if (settings_crc(&ns) != settings_crc(settings_get())) {
        uint32_t bad = settings_save(&ns); /* before the ACK: durable */
        if (bad)
            return err(LINK_CMD_CONFIG, LINK_E_FLASH, bad);
        link_stats.settings_writes++;
    }
    return ack(LINK_CMD_CONFIG, 0);
}

static bool sector_blank(uint32_t addr)
{
    const uint32_t *w = FLASH_MEM(addr);
    for (uint32_t i = 0; i < FLASH_SECTOR_BYTES / 4u; i++)
        if (w[i] != 0xFFFFFFFFu)
            return false;
    return true;
}

/* Erase one sector unless it already reads blank. */
static bool erase(uint32_t addr)
{
    if (sector_blank(addr))
        return true;
    link_stats.erases++;
    return flash_erase_sector(addr);
}

static uint8_t h_img_begin(const uint8_t *p)
{
    if (L.fw_done)
        return err(LINK_CMD_IMG_BEGIN, LINK_E_STATE, LINK_FWS_STAGED);
    uint64_t id = link_get64(p + LINK_IMGB_O_FRAME_ID);
    uint8_t bpp = p[LINK_IMGB_O_BPP], enc = p[LINK_IMGB_O_ENCODING];
    uint32_t len = link_get32(p + LINK_IMGB_O_LENGTH);
    if (!id)
        return err(LINK_CMD_IMG_BEGIN, LINK_E_BAD_VALUE, LINK_IMGB_O_FRAME_ID);
    if (bpp != 1u && bpp != 2u)
        return err(LINK_CMD_IMG_BEGIN, LINK_E_BAD_VALUE, LINK_IMGB_O_BPP);
    if (enc != 0u)
        return err(LINK_CMD_IMG_BEGIN, LINK_E_BAD_VALUE, LINK_IMGB_O_ENCODING);
    if (len > IMG_MAX || len != bpp * FRAME_PLANE_BYTES)
        return err(LINK_CMD_IMG_BEGIN, LINK_E_TOO_BIG, IMG_MAX);

    const frame_hdr_t *act = frame_active();
    L.img_open = false;
    L.img_fw = false;
    if (act && frame_id(act) == id) {
        RSP[LINK_ACK_O_DATA] = LINK_IMG_HAVE;
        return ack(LINK_CMD_IMG_BEGIN, 1);
    }
    uint32_t slot = act == (const frame_hdr_t *)FLASH_MEM(FRAME_SLOT_A) ? FRAME_SLOT_B : FRAME_SLOT_A;
    if (!erase(slot))
        return err(LINK_CMD_IMG_BEGIN, LINK_E_FLASH, slot);
    uint32_t hdr[FRAME_HDR_BYTES / 4u];
    uint8_t *h = (uint8_t *)hdr;
    link_put32(h, FRAME_MAGIC);
    h[4] = FRAME_VERSION;
    h[5] = bpp;
    h[6] = enc;
    h[7] = p[LINK_IMGB_O_FLAGS];
    link_put32(h + 8, act ? act->seq + 1u : 1u);
    link_put64(h + 12, id);
    link_put32(h + 20, len);
    link_put32(h + 24, link_get32(p + LINK_IMGB_O_CRC32));
    link_put32(h + 28, 0xFFFFFFFFu); /* commit word stays erased until IMG_END */
    if (!flash_program(slot, h, FRAME_HDR_BYTES))
        return err(LINK_CMD_IMG_BEGIN, LINK_E_FLASH, slot);
    link_stats.programs++;
    L.img_open = true;
    L.img_slot = slot;
    L.img_id = id;
    L.img_len = len;
    L.img_crc = link_get32(p + LINK_IMGB_O_CRC32);
    L.img_next = 0;
    L.img_erased_end = slot + FLASH_SECTOR_BYTES;
    RSP[LINK_ACK_O_DATA] = LINK_IMG_START;
    return ack(LINK_CMD_IMG_BEGIN, 1);
}

static uint8_t ack_next(void)
{
    link_put32(RSP + LINK_ACK_O_DATA, L.img_next);
    return ack(LINK_CMD_IMG_DATA, 4);
}

static uint8_t h_img_data(const uint8_t *p, uint32_t n)
{
    if (!L.img_open)
        return err(LINK_CMD_IMG_DATA, LINK_E_STATE, 0);
    uint32_t off = link_get32(p + LINK_IMGD_O_OFFSET), k = n - LINK_IMGD_O_DATA;
    if (off > L.img_len || k > L.img_len - off)
        return err(LINK_CMD_IMG_DATA, LINK_E_TOO_BIG, L.img_len);
    uint32_t end = off + k;
    if (end <= L.img_next)
        return ack_next(); /* a late repeat with a new seq: already written */
    if (off != L.img_next)
        return err(LINK_CMD_IMG_DATA, LINK_E_OFFSET, L.img_next);
    if (k % 4u && end != L.img_len)
        return err(LINK_CMD_IMG_DATA, LINK_E_BAD_LEN, 4);

    uint32_t top = L.img_slot + FRAME_HDR_BYTES + end;
    while (L.img_erased_end < top) { /* lazy, per sector: <= 3 per chunk */
        if (!erase(L.img_erased_end))
            return err(LINK_CMD_IMG_DATA, LINK_E_FLASH, L.img_erased_end);
        L.img_erased_end += FLASH_SECTOR_BYTES;
    }
    uint32_t dst = L.img_slot + FRAME_HDR_BYTES + off;
    if (!flash_program(dst, p + LINK_IMGD_O_DATA, k))
        return err(LINK_CMD_IMG_DATA, LINK_E_FLASH, dst);
    link_stats.programs++;
    L.img_next = end;
    return ack_next();
}

static uint8_t h_img_end(const uint8_t *p)
{
    if (!L.img_open || L.img_fw)
        return err(LINK_CMD_IMG_END, LINK_E_STATE, 0);
    if (link_get64(p) != L.img_id)
        return err(LINK_CMD_IMG_END, LINK_E_BAD_VALUE, 0);
    if (L.img_next != L.img_len)
        return err(LINK_CMD_IMG_END, LINK_E_OFFSET, L.img_next);
    L.img_open = false; /* no resume either way (4.9) */
    feed();
    uint32_t crc = frame_crc32(0, FLASH_MEM(L.img_slot + FRAME_HDR_BYTES), L.img_len);
    if (crc != L.img_crc)
        return err(LINK_CMD_IMG_END, LINK_E_CRC, crc);
    const uint32_t zero = FRAME_COMMITTED;
    if (!flash_program(L.img_slot + 28u, &zero, 4))
        return err(LINK_CMD_IMG_END, LINK_E_FLASH, L.img_slot + 28u);
    frame_invalidate();
    s_out.committed = true;
    return ack(LINK_CMD_IMG_END, 0);
}

/* ---- MCU firmware (4.14-4.15, design 4.7) ---- */

/* The bootloader's version from its info block, 0 if there is none (an app
 * flashed at 0 by an old tool, or the host model without one). */
static uint8_t boot_ver(void)
{
    uintptr_t at = FWU_BOOT_BASE + FWU_INFO_OFF;
    __asm volatile("" : "+r"(at)); /* GCC flags constant reads near address 0 */
    const fwu_info_t *b = FLASH_MEM(at);
    return b->magic == FWU_BOOT_MAGIC && b->version <= 0xFFu ? (uint8_t)b->version : 0u;
}

static uint8_t h_fw_begin(const uint8_t *p)
{
    uint32_t ver = link_get32(p + LINK_FWB_O_VERSION), len = link_get32(p + LINK_FWB_O_LENGTH);
    uint32_t crc = link_get32(p + LINK_FWB_O_CRC32);
    if (!boot_ver())
        return err(LINK_CMD_FW_BEGIN, LINK_E_STATE, LINK_FWS_NO_BOOT);
    if (L.fw_done)
        return err(LINK_CMD_FW_BEGIN, LINK_E_STATE, LINK_FWS_STAGED);
    if (link_get32(p + LINK_FWB_O_LOAD) != FWU_APP_BASE)
        return err(LINK_CMD_FW_BEGIN, LINK_E_BAD_VALUE, LINK_FWB_O_LOAD);
    if (len < FWU_MIN_IMAGE || len > FWU_MAX_IMAGE || len % 4u)
        return err(LINK_CMD_FW_BEGIN, LINK_E_TOO_BIG, FWU_MAX_IMAGE);

    L.img_open = false;
    feed();
    if (ver == LINK_FW_VERSION && frame_crc32(0, FLASH_MEM(FWU_APP_BASE), len) == crc) {
        RSP[LINK_ACK_O_DATA] = LINK_IMG_HAVE;
        return ack(LINK_CMD_FW_BEGIN, 1);
    }
    /* The image overwrites both frame slots: invalidate both headers now,
     * so an abandoned transfer never leaves a half-overwritten frame. The
     * cache first, so every path below (also a failed erase) drops it. */
    frame_invalidate();
    if (!erase(FRAME_SLOT_B))
        return err(LINK_CMD_FW_BEGIN, LINK_E_FLASH, FRAME_SLOT_B);
    if (!erase(FWU_STAGE))
        return err(LINK_CMD_FW_BEGIN, LINK_E_FLASH, FWU_STAGE);
    fwu_hdr_t h = {
        .magic = FWU_MAGIC, .fw_version = ver, .length = len, .crc32 = crc,
        .load = FWU_APP_BASE, .min_boot_ver = FWU_BOOT_VER,
        .commit = FWU_ERASED, .done = FWU_ERASED,
    };
    if (!flash_program(FWU_STAGE, &h, sizeof h))
        return err(LINK_CMD_FW_BEGIN, LINK_E_FLASH, FWU_STAGE);
    link_stats.programs++;
    L.img_open = true;
    L.img_fw = true;
    L.img_slot = FWU_STAGE;
    L.img_id = 0;
    L.img_len = len;
    L.img_crc = crc;
    L.img_next = 0;
    L.img_erased_end = FWU_STAGE + FLASH_SECTOR_BYTES;
    RSP[LINK_ACK_O_DATA] = LINK_IMG_START;
    return ack(LINK_CMD_FW_BEGIN, 1);
}

static uint8_t h_fw_end(const uint8_t *p)
{
    if (!L.img_open || !L.img_fw)
        return err(LINK_CMD_FW_END, LINK_E_STATE, 0);
    if (link_get32(p) != L.img_crc)
        return err(LINK_CMD_FW_END, LINK_E_BAD_VALUE, 0);
    if (L.img_next != L.img_len)
        return err(LINK_CMD_FW_END, LINK_E_OFFSET, L.img_next);
    L.img_open = false;
    feed();
    uint32_t crc = frame_crc32(0, FLASH_MEM(FWU_STAGE + FWU_HDR_BYTES), L.img_len);
    if (crc != L.img_crc)
        return err(LINK_CMD_FW_END, LINK_E_CRC, crc);
    const uint32_t zero = FWU_SET;
    uint32_t a = FWU_STAGE + offsetof(fwu_hdr_t, commit);
    if (!flash_program(a, &zero, 4))
        return err(LINK_CMD_FW_END, LINK_E_FLASH, a);
    L.fw_done = true;
    s_out.fw_staged = true;
    return ack(LINK_CMD_FW_END, 0);
}

static uint8_t h_hold(const uint8_t *p, uint32_t now)
{
    uint32_t s = link_get16(p);
    if (s < 1u || s > LINK_T_HOLD_MAX_S)
        return err(LINK_CMD_HOLD, LINK_E_BAD_VALUE, 0);
    /* After a NET failure only the 2 s grace for BYE is left: a HOLD there
     * must not keep a failed CBU powered (protocol.md 4.4). */
    if (L.state == LINK_ST_GRACE)
        return err(LINK_CMD_HOLD, LINK_E_STATE, 0);
    L.holding = true;
    L.hold_seen = true;
    uint32_t rel = now - L.t_on + s * 1000u, cap = LINK_T_HOLD_MAX_S * 1000u;
    if (rel > cap)
        rel = cap;
    if (rel > L.deadline - L.t_on)
        L.deadline = L.t_on + rel;
    uint32_t left = due(now, L.deadline) ? 0u : (L.deadline - now) / 1000u;
    link_put16(RSP + LINK_ACK_O_DATA, (uint16_t)left);
    return ack(LINK_CMD_HOLD, 2);
}

static uint8_t h_bye(const uint8_t *p, uint32_t n, uint32_t now)
{
    uint8_t reason = p[0];
    if (L.state != LINK_ST_GRACE && reason == LINK_BYE_DONE && n >= 2u &&
        (p[1] & LINK_BYE_F_HISTORY_ACK)) history_ack(L.history_token);
    if (L.state != LINK_ST_GRACE)
        L.result_pending = reason == LINK_BYE_DONE || reason == LINK_BYE_FORGOT
                         ? LINK_RES_OK : LINK_RES_NET_FAIL;
    L.state = LINK_ST_CLOSING;
    L.closing_at = now;
    L.r_after = AFTER_BYE;
    return ack(LINK_CMD_BYE, 0);
}

static int32_t min_len(uint8_t cmd)
{
    switch (cmd) {
    case LINK_CMD_HELLO: return LINK_HELLO_LEN;
    case LINK_CMD_PING: return 0;
    case LINK_CMD_NET: return LINK_NET_LEN;
    case LINK_CMD_TIME: return LINK_TIME_LEN;
    case LINK_CMD_CONFIG: return 0;
    case LINK_CMD_IMG_BEGIN: return LINK_IMG_BEGIN_LEN;
    case LINK_CMD_IMG_DATA: return LINK_IMGD_O_DATA + 1;
    case LINK_CMD_IMG_END: return LINK_IMG_END_LEN;
    case LINK_CMD_FW_BEGIN: return LINK_FW_BEGIN_LEN;
    case LINK_CMD_FW_END: return LINK_FW_END_LEN;
    case LINK_CMD_HOLD: return LINK_HOLD_LEN;
    case LINK_CMD_BYE: return LINK_BYE_LEN;
    default: return -1;
    }
}

static uint8_t handle(uint8_t cmd, const uint8_t *p, uint32_t n, uint32_t now)
{
    L.r_after = AFTER_NONE;
    int32_t ml = min_len(cmd);
    if (ml < 0)
        return err(cmd, LINK_E_UNKNOWN_CMD, 0);
    if (n < (uint32_t)ml)
        return err(cmd, LINK_E_BAD_LEN, (uint32_t)ml);
    /* After BYE the CBU is about to lose power: a HELLO there (a CBU that
     * rebooted after BYE) is refused, so the session still ends as BYE
     * said (protocol.md 5.3). */
    if (cmd == LINK_CMD_HELLO && L.state != LINK_ST_CLOSING)
        return h_hello(p);
    if (L.state == LINK_ST_WAIT_HELLO || L.state == LINK_ST_CLOSING)
        return err(cmd, LINK_E_STATE, 0);
    switch (cmd) {
    case LINK_CMD_PING:
        if (n > LINK_PING_MAX)
            return err(cmd, LINK_E_BAD_LEN, LINK_PING_MAX);
        memcpy(RSP + LINK_ACK_O_DATA, p, n);
        return ack(cmd, (uint16_t)n);
    case LINK_CMD_NET: return h_net(p, now);
    case LINK_CMD_TIME: return h_time(p);
    case LINK_CMD_CONFIG: return h_config(p, n);
    case LINK_CMD_IMG_BEGIN: return h_img_begin(p);
    case LINK_CMD_IMG_DATA: return h_img_data(p, n);
    case LINK_CMD_IMG_END: return h_img_end(p);
    case LINK_CMD_FW_BEGIN: return h_fw_begin(p);
    case LINK_CMD_FW_END: return h_fw_end(p);
    case LINK_CMD_HOLD: return h_hold(p, now);
    default: return h_bye(p, n, now);
    }
}

static void on_request(uint8_t seq, uint8_t cmd, const uint8_t *p, uint32_t n, uint16_t crc,
                       uint32_t now)
{
    if (cmd != LINK_CMD_HELLO && L.dup_valid && seq == L.dup_seq && cmd == L.dup_cmd &&
        crc == L.dup_crc) {
        link_stats.dup_requests++;
        lport_write(L.dup_buf, L.dup_len); /* re-ACK, do not re-apply (3.5) */
        return;
    }
    uint8_t rcmd = handle(cmd, p, n, now);
    uint32_t len = link_frame_seal(L.tx, seq, rcmd, L.r_len);
    L.dup_valid = len <= DUP_MAX;
    if (L.dup_valid) {
        L.dup_seq = seq;
        L.dup_cmd = cmd;
        L.dup_crc = crc;
        L.dup_len = (uint8_t)len;
        memcpy(L.dup_buf, L.tx, len);
    }
    lport_write(L.tx, len);
    if (L.r_after)
        L.after = L.r_after;
}

static void on_response(uint8_t seq, uint8_t cmd, const uint8_t *p, uint32_t n)
{
    if (!L.out_active || seq != L.out_seq)
        return; /* stale duplicate: ignore */
    if ((cmd == LINK_CMD_ACK || cmd == LINK_CMD_ERR) && (n < 1u || p[0] != L.out_cmd))
        return;
    if (cmd == LINK_CMD_HELLO_RSP)
        return;
    L.out_active = false;
    if (L.out_cmd == LINK_CMD_FORGET && cmd == LINK_CMD_ACK)
        L.forget_pending = false;
}

static void send_request(uint8_t cmd)
{
    L.out_seq = L.tx_seq++;
    L.out_cmd = cmd;
    (void)link_frame_seal(L.out_raw, L.out_seq, cmd, 0);
    L.out_active = true;
    L.out_deadline_set = false;
    L.out_attempts = 1;
    lport_write(L.out_raw, LINK_OVERHEAD);
}

/* Frames from the buffer (3.4). */
static void scan(uint32_t now)
{
    while (L.rx_n) {
        if (L.rx[0] != LINK_SOF) {
            uint32_t i = 1;
            while (i < L.rx_n && L.rx[i] != LINK_SOF)
                i++;
            rx_drop(i);
            continue;
        }
        if (L.rx_n < LINK_HDR_BYTES)
            return;
        uint32_t len = link_get16(L.rx + 4);
        if (L.rx[1] != LINK_VER || len > LINK_MAX_TO_MCU) {
            rx_drop(1);
            continue;
        }
        uint32_t fl = LINK_OVERHEAD + len;
        if (L.rx_n < fl)
            return;
        uint16_t crc = link_get16(L.rx + LINK_HDR_BYTES + len);
        if (link_crc16(0xFFFFu, L.rx + 1, LINK_HDR_BYTES - 1u + len) != crc) {
            rx_drop(1); /* only the A5: a real frame may hide behind it */
            continue;
        }
        L.have_valid = true;
        L.last_valid = now;
        uint8_t seq = L.rx[2], cmd = L.rx[3];
        if (cmd & LINK_CMD_RESPONSE)
            on_response(seq, cmd, L.rx + LINK_HDR_BYTES, len);
        else
            on_request(seq, cmd, L.rx + LINK_HDR_BYTES, len, crc, now);
        if (L.state == LINK_ST_PROBE)
            L.state = LINK_ST_LINKED; /* a valid frame at 500 k: keep it */
        if (L.rx_n >= fl) {
            memmove(L.rx, L.rx + fl, L.rx_n - fl);
            L.rx_n -= fl;
        }
    }
}

static void end_session(uint32_t now, uint8_t result);

static void rx_poll(uint32_t now)
{
    uint32_t disc0 = link_stats.discarded;
    if (L.rx_n && now - L.last_byte > LINK_T_INTERBYTE_MS) {
        rx_drop(1); /* a stale partial frame: drop its A5 and rescan */
        scan(now);
    }
    int c;
    while (L.cbu_on && (c = lport_getc()) >= 0) {
        if (L.state == LINK_ST_WAIT_HELLO && tuya_push((uint8_t)c)) {
            end_session(now, LINK_RES_TUYA);
            return;
        }
        L.rx[L.rx_n++] = (uint8_t)c; /* scan() keeps rx_n < LINK_RX_BUF here */
        L.last_byte = now;
        scan(now);
    }
    if (link_stats.discarded > disc0 && L.baud == LINK_BAUD_FAST) {
        uint32_t ref = L.have_valid ? L.last_valid : L.fast_since;
        if (now - ref >= LINK_T_GARBAGE_MS)
            revert(now); /* R1b */
    }
}

/* ---- session ---------------------------------------------------------------- */

static void end_session(uint32_t now, uint8_t result)
{
    lport_power(false);
    hw_session(false);
    L.cbu_on = false;
    L.state = LINK_ST_OFF;
    L.img_open = false;
    L.after = AFTER_NONE;
    L.out_active = false;
    L.rx_n = 0;
    L.last_session_ms = now - L.t_on;
    L.last_result = result;

    link_outcome_t *o = &s_out;
    /* A maintenance session (the CBU was granted HOLD: OTA, adoption) or a
     * bench session that saw a HELLO normally ends at the hold cap. That is
     * the expected end, not a link failure: scheduled like OK, counters and
     * icon kept as for ABORT. The result code stays CAP for HA. */
    bool maint = result == LINK_RES_CAP && (L.hold_seen || (L.bench && L.got_hello));
    if (maint) {
        /* icon, status and counters unchanged */
    } else if (result == LINK_RES_OK) {
        o->fail_count = o->nohello_count = 0;
        o->icon = LINK_OK;
        o->status = LINK_STATUS_NONE;
        L.factory = false;
    } else if (result == LINK_RES_NO_HELLO || result == LINK_RES_TUYA) {
        if (o->nohello_count < 255u) o->nohello_count++;
        if (o->fail_count < 255u) o->fail_count++;
        o->icon = LINK_ERR;
        o->status = LINK_STATUS_LINK_ERROR;
    } else if (result != LINK_RES_ABORT) {
        if (o->fail_count < 255u) o->fail_count++;
        o->icon = LINK_ERR;
        o->status = net_status(result);
    }
    o->next_s = link_next_after(maint ? LINK_RES_OK : result,
                                (uint32_t)settings_get()->v[LINK_CFG_SYNC_MIN],
                                o->fail_count, o->nohello_count);
    o->result = result;
    o->session_ms = L.last_session_ms;
    o->sessions++;
}

void link_init(uint8_t reset_cause)
{
    history_init();
    L.reset_cause = reset_cause;
    settings_init();
    L.factory = settings_factory();
    lport_init();
}

bool link_start(uint32_t now, uint8_t trigger, const link_reading_t *r, bool bench)
{
    if (L.cbu_on)
        return true;
    /* Low battery at rest and not charging: skip (not a failure). 0 mV =
     * no reading, which does not block a session. */
    if (r->batt_mv && r->batt_mv < LINK_BATT_MIN_MV && r->charge == LINK_CHG_DISCHARGING)
        return false;
    L.rd = *r;
    L.trigger = bench ? (uint8_t)LINK_TRIG_BENCH : trigger;
    L.bench = bench;
    L.holding = bench;
    L.hold_seen = false;
    L.got_hello = false;
    s_out.committed = false;
    s_out.fw_staged = false;
    L.fw_done = false;

    hw_session(true);
    L.baud = LINK_BAUD_SLOW;
    L.sticky_slow = false;
    L.rx_n = 0;
    L.tuya_n = 0;
    L.dup_valid = false;
    L.after = AFTER_NONE;
    L.out_active = false;
    L.tx_seq = 0;
    L.img_open = false;
    L.forget_sent = false;
    L.have_valid = false;
    L.off_pending = false;
    L.baud_pending = false;
    L.result_pending = LINK_RES_OK;
    L.net_state = 0;
    lport_power(true); /* port: pins, RX on, 115200, then PB05 low (2.2) */

    L.cbu_on = true;
    L.state = LINK_ST_WAIT_HELLO;
    L.t_on = now;
    L.hello_deadline = now + LINK_T_HELLO_MS;
    L.deadline = now + (bench ? LINK_T_HOLD_MAX_S * 1000u : LINK_T_CAP_MS);
    return true;
}

bool link_active(void) { return L.cbu_on; }

void link_factory_reset(void)
{
    L.forget_pending = true; /* RAM only: survives deep sleep, not a reset (4.12) */
    L.factory = true;
    s_out.icon = LINK_WAIT;
    s_out.status = LINK_STATUS_NOT_SET_UP;
}

void link_abort(uint32_t now)
{
    if (L.cbu_on)
        end_session(now, LINK_RES_ABORT);
}

void link_poll(uint32_t now)
{
    if (!L.cbu_on)
        return;
    rx_poll(now);
    if (!L.cbu_on)
        return;
    apply_baud();
    if (L.after && lport_tx_idle()) {
        uint8_t a = L.after;
        L.after = AFTER_NONE;
        if (a == AFTER_FAST) {
            set_baud(LINK_BAUD_FAST, now);
            L.state = LINK_ST_PROBE;
            L.probe_deadline = now + LINK_T_PROBE_MS;
        } else if (a == AFTER_LINKED) {
            L.state = LINK_ST_LINKED;
        } else {
            L.off_pending = true;
            L.off_at = now + LINK_T_BYE_OFF_MS;
        }
    }
    if (L.out_active) {
        if (!L.out_deadline_set) {
            if (lport_tx_idle()) {
                L.out_deadline_set = true;
                L.out_deadline = now + LINK_T_ACK_MS;
            }
        } else if (due(now, L.out_deadline)) {
            if (L.out_attempts > LINK_RETRIES) {
                L.out_active = false; /* failed: FORGET stays pending */
            } else {
                if (L.baud == LINK_BAUD_FAST)
                    revert(now); /* R1a */
                L.out_attempts++;
                L.out_deadline_set = false;
                lport_write(L.out_raw, LINK_OVERHEAD);
            }
        }
    }
    if (L.off_pending && due(now, L.off_at)) {
        end_session(now, L.result_pending);
        return;
    }
    if (L.state == LINK_ST_CLOSING) {
        /* Normally off_at (TC + 50 ms) ends it. If TC never comes, or the
         * deadline passes first, end with BYE's result anyway: the CBU
         * must not stay powered, nor the MCU at 24 MHz. */
        if (due(now, L.closing_at + LINK_T_BYE_TC_MAX_MS) || due(now, L.deadline))
            end_session(now, L.result_pending);
        return;
    }
    if (due(now, L.deadline)) {
        end_session(now, L.state == LINK_ST_GRACE ? LINK_RES_NET_FAIL : LINK_RES_CAP);
        return;
    }
    if (L.state == LINK_ST_WAIT_HELLO && !L.holding && due(now, L.hello_deadline)) {
        end_session(now, LINK_RES_NO_HELLO);
        return;
    }
    if (L.state == LINK_ST_PROBE && due(now, L.probe_deadline)) {
        revert(now);
        L.state = LINK_ST_LINKED;
    }
    if (L.state == LINK_ST_LINKED && L.forget_pending && !L.forget_sent && !L.out_active) {
        L.forget_sent = true;
        send_request(LINK_CMD_FORGET);
    }
}

uint32_t link_next_deadline(uint32_t now)
{
    uint32_t best = now + 1000u;
#define CAND(t) do { if ((int32_t)((t) - best) < 0) best = (t); } while (0)
    if (!L.cbu_on)
        return best;
    CAND(L.deadline);
    if (L.state == LINK_ST_WAIT_HELLO && !L.holding)
        CAND(L.hello_deadline);
    if (L.state == LINK_ST_PROBE)
        CAND(L.probe_deadline);
    if (L.off_pending)
        CAND(L.off_at);
    if (L.state == LINK_ST_CLOSING)
        CAND(L.closing_at + LINK_T_BYE_TC_MAX_MS);
    if (L.out_active && L.out_deadline_set)
        CAND(L.out_deadline);
    if (L.rx_n)
        CAND(L.last_byte + LINK_T_INTERBYTE_MS + 1u);
#undef CAND
    return best;
}

const link_outcome_t *link_outcome(void) { return &s_out; }
