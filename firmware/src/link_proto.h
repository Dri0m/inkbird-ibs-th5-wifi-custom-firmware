/* MCU <-> CBU link protocol, shared with the Python reference implementation
 * (tools/th5link/proto.py). The binding spec is docs/protocol.md; section
 * numbers below refer to it.
 *
 * Frame (3.1):
 *
 *   off  size  field
 *    0    1    sof      0xA5
 *    1    1    ver      LINK_VER
 *    2    1    seq      chosen by the requester; a response echoes it
 *    3    1    cmd      LINK_CMD_*; bit 7 set = response (ACK, HELLO_RSP, ERR)
 *    4    2    len      payload bytes, little-endian
 *    6    len  payload
 *   6+len 2    crc      CRC-16/CCITT-FALSE over bytes 1 .. 5+len (ver through
 *                       the last payload byte; not the 0xA5), little-endian
 *
 * Every multi-byte field is little-endian. Payloads are described as byte
 * offsets and read with link_get*() / written with link_put*(): no packed
 * structs. Fixed payloads may grow at the end in later versions; receivers
 * accept len >= the LINK_*_LEN given here and ignore the tail (4.0).
 *
 * Freestanding: no libc. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* --- Framing (3) ---------------------------------------------------------- */
#define LINK_SOF            0xA5u
#define LINK_VER            1u
#define LINK_HDR_BYTES      6u
#define LINK_CRC_BYTES      2u
#define LINK_OVERHEAD       (LINK_HDR_BYTES + LINK_CRC_BYTES)
#define LINK_MAX_TO_MCU     1024u       /* CBU -> MCU payload cap */
#define LINK_MAX_TO_CBU     256u        /* MCU -> CBU payload cap */
#define LINK_RX_BUF         (LINK_MAX_TO_MCU + LINK_OVERHEAD)   /* MCU side */
#define LINK_TX_BUF         (LINK_MAX_TO_CBU + LINK_OVERHEAD)

/* --- Physical layer and timing (2, 3.5, 5) -------------------------------- */
#define LINK_BAUD_SLOW      115200u
#define LINK_BAUD_FAST      500000u     /* HC32 24 MHz /8 SCNT 6; BK 26 MHz /52 */
#define LINK_T_ACK_MS       200u        /* response timeout */
#define LINK_T_ACK_LONG_MS  1000u       /* ... for IMG_BEGIN and IMG_END */
#define LINK_RETRIES        3u          /* resends after the first attempt */
#define LINK_T_REPLY_MS     150u        /* responder budget (IMG_*: 800) */
#define LINK_T_REPLY_LONG_MS 800u
#define LINK_T_PROBE_MS     100u        /* baud probe window */
#define LINK_T_PROBE_GAP_MS 2u          /* CBU: HELLO_RSP received -> PING */
#define LINK_T_SETTLE_MS    10u         /* CBU: reverted -> next byte */
#define LINK_T_GARBAGE_MS   50u         /* R1b: garbage this long after the last valid frame */
#define LINK_T_INTERBYTE_MS 20u         /* partial frame dropped after this gap */
#define LINK_T_HELLO_MS     5000u
#define LINK_T_CAP_MS       45000u
#define LINK_T_HOLD_MAX_S   900u
#define LINK_T_BYE_OFF_MS   50u
#define LINK_T_BYE_TC_MAX_MS 100u       /* CLOSING ends this long after the BYE ACK at the latest (5.1) */
#define LINK_T_NET_GRACE_MS 2000u

/* --- Commands (4) --------------------------------------------------------- */
enum {
    LINK_CMD_HELLO     = 0x01, /* CBU -> MCU */
    LINK_CMD_PING      = 0x02, /* either */
    LINK_CMD_NET       = 0x03, /* CBU -> MCU */
    LINK_CMD_TIME      = 0x04,
    LINK_CMD_CONFIG    = 0x05,
    LINK_CMD_IMG_BEGIN = 0x06,
    LINK_CMD_IMG_DATA  = 0x07,
    LINK_CMD_IMG_END   = 0x08,
    LINK_CMD_HOLD      = 0x09,
    LINK_CMD_BYE       = 0x0A,
    LINK_CMD_FW_BEGIN  = 0x0B, /* CBU -> MCU: MCU firmware (fwu.h) */
    LINK_CMD_FW_END    = 0x0C,
    LINK_CMD_FORGET    = 0x40, /* MCU -> CBU */
    LINK_CMD_ACK       = 0x80, /* responses: never answered */
    LINK_CMD_HELLO_RSP = 0x81,
    LINK_CMD_ERR       = 0x82,
};
#define LINK_CMD_RESPONSE   0x80u

/* ERR codes (4.13) */
enum {
    LINK_E_UNKNOWN_CMD = 1, /* detail 0 */
    LINK_E_BAD_LEN     = 2, /* detail: minimum length, or TLV offset (CONFIG) */
    LINK_E_BAD_VALUE   = 3, /* detail: payload offset of the field, or CONFIG tag */
    LINK_E_VERSION     = 4, /* detail: highest version the responder speaks */
    LINK_E_STATE       = 5, /* detail 0: not valid now */
    LINK_E_OFFSET      = 6, /* detail: next offset the MCU expects */
    LINK_E_TOO_BIG     = 7, /* detail: largest acceptable length */
    LINK_E_CRC         = 8, /* detail: CRC-32 the MCU computed */
    LINK_E_FLASH       = 9, /* detail: flash address that failed verify */
    LINK_E_BUSY        = 10,/* detail 0: retry later with a new seq */
};

/* --- Payloads (4): offsets and lengths ------------------------------------ */
/* HELLO (CBU -> MCU) */
#define LINK_HELLO_O_PROTO_MAX   0  /* u8 */
#define LINK_HELLO_O_FLAGS       1  /* u8 LINK_HELLO_F_*; other bits 0 */
#define LINK_HELLO_F_SLOW_ONLY   0x01u /* the CBU stays at 115200: offer no 500 k */
#define LINK_HELLO_O_RX_MAX      2  /* u16 */
#define LINK_HELLO_O_FW          4  /* u32 */
#define LINK_HELLO_LEN           8

/* HELLO_RSP (MCU -> CBU, the response to HELLO) */
#define LINK_HR_O_PROTO          0  /* u8  version for the rest of the session */
#define LINK_HR_O_RESET_CAUSE    1  /* u8  LINK_RST_* bits */
#define LINK_HR_O_BAUD           2  /* u32 rate after this frame */
#define LINK_HR_O_RX_MAX         6  /* u16 */
#define LINK_HR_O_FW             8  /* u32 */
#define LINK_HR_O_FRAME_ID      12  /* u64 0 = no valid frame */
#define LINK_HR_O_SETTINGS_CRC  20  /* u32 */
#define LINK_HR_O_UNITS         24  /* u8  0 degC, 1 degF */
#define LINK_HR_O_TRIGGER       25  /* u8  LINK_TRIG_* */
#define LINK_HR_O_FLAGS         26  /* u16 LINK_F_* */
#define LINK_HR_O_T             28  /* i16 0.01 degC, LINK_T_NONE */
#define LINK_HR_O_RH            30  /* u16 0.01 %RH, LINK_RH_NONE */
#define LINK_HR_O_BATT_MV       32  /* u16 at rest, before PB05 went low */
#define LINK_HR_O_BATT_PCT      34  /* u8  0..100, 0xFF unknown */
#define LINK_HR_O_CHARGE        35  /* u8  LINK_CHG_* */
#define LINK_HR_O_LAST_MS       36  /* u32 CBU-on time of the previous session */
#define LINK_HR_O_LAST_RESULT   40  /* u8  LINK_RES_* */
#define LINK_HR_O_FAIL_COUNT    41  /* u8  consecutive failed sessions */
#define LINK_HR_O_BOOT_VER      42  /* u8  bootloader version, 0 = none */
#define LINK_HR_O_RESERVED      43  /* u8  0 */
#define LINK_HELLO_RSP_LEN      44

enum { LINK_RST_POR = 1, LINK_RST_BOR = 2, LINK_RST_LVD = 4, LINK_RST_WDT = 8,
       LINK_RST_PIN = 16, LINK_RST_SOFT = 32, LINK_RST_LOCKUP = 64, LINK_RST_OTHER = 128 };
enum { LINK_TRIG_TIMER, LINK_TRIG_BUTTON, LINK_TRIG_UNITS, LINK_TRIG_BOOT,
       LINK_TRIG_CHARGE, LINK_TRIG_BENCH, LINK_TRIG_RETRY };
enum { LINK_F_TIME_VALID = 1, LINK_F_FRAME_VALID = 2, LINK_F_SENSOR_OK = 4,
       LINK_F_FORGET_PENDING = 8, LINK_F_BENCH = 16, LINK_F_FACTORY_RESET = 32 };
enum { LINK_CHG_DISCHARGING, LINK_CHG_CHARGING, LINK_CHG_FULL, LINK_CHG_UNKNOWN };
enum { LINK_RES_NONE, LINK_RES_OK, LINK_RES_NO_HELLO, LINK_RES_TUYA, LINK_RES_NET_FAIL,
       LINK_RES_CAP, LINK_RES_LINK, LINK_RES_ABORT };
#define LINK_T_NONE   ((int16_t)-32768)
#define LINK_RH_NONE  0xFFFFu
#define LINK_PCT_NONE 0xFFu

/* PING: 0..16 bytes, echoed in the ACK */
#define LINK_PING_MAX 16

/* NET (CBU -> MCU) */
#define LINK_NET_O_STATE   0  /* u8 LINK_NET_*; bit 7 = failure */
#define LINK_NET_O_DETAIL  1  /* u8 e.g. the Wi-Fi disconnect reason */
#define LINK_NET_O_RSSI    2  /* i8 dBm, 0 unknown */
#define LINK_NET_LEN       3
enum { LINK_NET_WIFI_CONNECTING = 1, LINK_NET_WIFI_UP, LINK_NET_API_UP, LINK_NET_HA_CONFIG,
       LINK_NET_OTA,
       LINK_NET_FAIL_WIFI = 0x80, LINK_NET_FAIL_API, LINK_NET_FAIL_NO_HA,
       LINK_NET_FAIL_NO_CREDENTIALS };
#define LINK_NET_FAILED    0x80u

/* TIME (CBU -> MCU) */
#define LINK_TIME_O_UTC          0  /* u32 s since 1970 UTC, at the first byte */
#define LINK_TIME_O_OFFSET       4  /* i16 minutes, local = UTC + offset */
#define LINK_TIME_O_NEXT_UTC     6  /* u32 next transition, 0 = none known */
#define LINK_TIME_O_NEXT_OFFSET 10  /* i16 minutes from NEXT_UTC on */
#define LINK_TIME_LEN           12
#define LINK_TIME_UTC_MIN       1735689600u   /* 2025-01-01: older is refused */
#define LINK_TIME_UTC_MAX       4102444800u   /* 2100-01-01: the RTC year is 2 BCD digits */
#define LINK_TIME_OFFSET_MAX    (18 * 60)

/* CONFIG (CBU -> MCU): TLV list {tag u8, len u8, value[len]}; unknown tags
 * are skipped; absent tags keep their stored value. */
enum {
    LINK_CFG_MODE      = 1, /* u8  0 picture, 1 forecast */
    LINK_CFG_SYNC_MIN  = 2, /* u16 15..240 */
    LINK_CFG_CLOCK_MIN = 3, /* u8  1..30 */
    LINK_CFG_CLOCK_24H = 4, /* u8  0 12 h, 1 24 h */
    LINK_CFG_FULL_N    = 5, /* u8  1..255 band updates per full refresh */
    LINK_CFG_T_OFF     = 6, /* i16 0.01 degC, -1000..1000 */
    LINK_CFG_RH_OFF    = 7, /* i16 0.01 %RH, -2000..2000 */
    LINK_CFG_FLAGS     = 8, /* u8  LINK_CFGF_* */
    LINK_CFG_TAG_COUNT = 8,
};
enum { LINK_CFGF_BADGE = 1, LINK_CFGF_MAINTENANCE = 2,
       LINK_CFGF_BAND_MONO = 4, LINK_CFGF_BAND_SINGLE_RESET = 8 };

/* IMG_BEGIN (CBU -> MCU); fields as in the slot header (frame.h) */
#define LINK_IMGB_O_FRAME_ID  0  /* u64, != 0 */
#define LINK_IMGB_O_BPP       8  /* u8 1|2 */
#define LINK_IMGB_O_ENCODING  9  /* u8 0 raw */
#define LINK_IMGB_O_FLAGS    10  /* u8 FRAME_FLAG_* */
#define LINK_IMGB_O_RESERVED 11  /* u8 0 */
#define LINK_IMGB_O_LENGTH   12  /* u32 payload bytes */
#define LINK_IMGB_O_CRC32    16  /* u32 zlib CRC-32 of the payload */
#define LINK_IMG_BEGIN_LEN   20
enum { LINK_IMG_START = 0, LINK_IMG_HAVE = 1 };   /* ACK data byte */

/* IMG_DATA: u32 offset, then 1..LINK_IMG_CHUNK data bytes. ACK data: u32 next. */
#define LINK_IMGD_O_OFFSET   0
#define LINK_IMGD_O_DATA     4
#define LINK_IMG_CHUNK       1020u
/* IMG_END: u64 frame_id. */
#define LINK_IMG_END_LEN     8
/* FW_BEGIN (CBU -> MCU): the package header's fields (fwu.h). ACK data:
 * LINK_IMG_START or LINK_IMG_HAVE. IMG_DATA offsets are image offsets. */
#define LINK_FWB_O_VERSION    0  /* u32 */
#define LINK_FWB_O_LENGTH     4  /* u32 FWU_MIN_IMAGE..FWU_MAX_IMAGE, multiple of 4 */
#define LINK_FWB_O_CRC32      8  /* u32 zlib CRC-32 of the image */
#define LINK_FWB_O_LOAD      12  /* u32 FWU_APP_BASE */
#define LINK_FW_BEGIN_LEN    16
/* FW_END: u32 crc32 (as FW_BEGIN). */
#define LINK_FW_END_LEN       4
/* STATE details for FW_BEGIN / IMG_BEGIN */
enum { LINK_FWS_NO_BOOT = 1, LINK_FWS_STAGED = 2 };
/* HOLD: u16 seconds 1..900. ACK data: u16 seconds granted from now. */
#define LINK_HOLD_LEN        2
/* BYE: u8 reason. */
#define LINK_BYE_LEN         1
#define LINK_BYE_F_HISTORY_ACK 1u /* optional second byte; only with DONE */
enum { LINK_BYE_DONE, LINK_BYE_NO_HA, LINK_BYE_NET_FAIL, LINK_BYE_LINK, LINK_BYE_FORGOT };
/* ACK: u8 acknowledged cmd, then command-specific data. */
#define LINK_ACK_O_CMD       0
#define LINK_ACK_O_DATA      1
/* ERR: u8 cmd, u8 code, u32 detail. */
#define LINK_ERR_O_CMD       0
#define LINK_ERR_O_CODE      1
#define LINK_ERR_O_DETAIL    2
#define LINK_ERR_LEN         6

/* --- Little-endian access -------------------------------------------------- */
static inline uint16_t link_get16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
static inline uint32_t link_get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static inline uint64_t link_get64(const uint8_t *p)
{
    return (uint64_t)link_get32(p) | (uint64_t)link_get32(p + 4) << 32;
}
static inline void link_put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static inline void link_put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static inline void link_put64(uint8_t *p, uint64_t v)
{
    link_put32(p, (uint32_t)v);
    link_put32(p + 4, (uint32_t)(v >> 32));
}

/* --- CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xorout.
 * Check value: "123456789" -> 0x29B1. A nibble at a time, 32 B of table. */
static inline uint16_t link_crc16(uint16_t crc, const uint8_t *p, uint32_t n)
{
    static const uint16_t t[16] = {
        0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
        0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF,
    };
    while (n--) {
        crc = (uint16_t)((crc << 4) ^ t[(crc >> 12) ^ (*p >> 4)]);
        crc = (uint16_t)((crc << 4) ^ t[(crc >> 12) ^ (*p & 15u)]);
        p++;
    }
    return crc;
}

/* Fill in the header and CRC around a payload already at buf + 6.
 * Returns the frame length. */
static inline uint32_t link_frame_seal(uint8_t *buf, uint8_t seq, uint8_t cmd, uint16_t len)
{
    buf[0] = LINK_SOF;
    buf[1] = LINK_VER;
    buf[2] = seq;
    buf[3] = cmd;
    link_put16(buf + 4, len);
    link_put16(buf + LINK_HDR_BYTES + len, link_crc16(0xFFFFu, buf + 1, LINK_HDR_BYTES - 1u + len));
    return LINK_OVERHEAD + len;
}

/* Check one complete frame of n bytes. Returns the payload length, or -1 if
 * the start byte, version, length (vs max_payload) or CRC is wrong. */
static inline int32_t link_frame_check(const uint8_t *buf, uint32_t n, uint32_t max_payload)
{
    if (n < LINK_OVERHEAD || buf[0] != LINK_SOF || buf[1] != LINK_VER)
        return -1;
    uint32_t len = link_get16(buf + 4);
    if (len > max_payload || n != LINK_OVERHEAD + len)
        return -1;
    if (link_crc16(0xFFFFu, buf + 1, LINK_HDR_BYTES - 1u + len) != link_get16(buf + LINK_HDR_BYTES + len))
        return -1;
    return (int32_t)len;
}

/* --- SWD mailbox (8): bench transport replacing UART0 ------------------------
 * Built with -DLINK_MBOX. The host (tools/th5link/transport.py) finds the
 * symbol `link_mbox` in the ELF and plays the CBU over SWD.
 *
 * Ownership: the MCU writes magic/version/ring_size (once, magic last),
 * h2m_tail, m2h_head, m2h[] and mcu_ctl; the host writes h2m[], h2m_head,
 * m2h_tail and host_ctl. Nobody else writes them. Indices are in
 * [0, LINK_MBOX_RING); a ring is empty when head == tail and holds at most
 * LINK_MBOX_RING - 1 bytes. Producers write the data, then the head; consumers
 * read the head, then the data, then write the tail. Every index is one
 * aligned 32-bit access. */
#define LINK_MBOX_MAGIC     0x4D354854u   /* "TH5M" */
#define LINK_MBOX_VERSION   1u
#define LINK_MBOX_RING      1536u
#define LINK_MBOX_HOST_BOOTED 1u          /* host_ctl: the "CBU" is powered and booted */
#define LINK_MBOX_MCU_CBU_ON  1u          /* mcu_ctl: PB05 would be low */
#define LINK_MBOX_MCU_FAST    2u          /* mcu_ctl: the MCU is "at 500 k" (informational) */

typedef struct {
    volatile uint32_t magic;      /*  0 */
    volatile uint16_t version;    /*  4 */
    volatile uint16_t ring_size;  /*  6 */
    volatile uint32_t h2m_head;   /*  8 host */
    volatile uint32_t h2m_tail;   /* 12 MCU */
    volatile uint32_t m2h_head;   /* 16 MCU */
    volatile uint32_t m2h_tail;   /* 20 host */
    volatile uint32_t host_ctl;   /* 24 host */
    volatile uint32_t mcu_ctl;    /* 28 MCU */
    volatile uint8_t h2m[LINK_MBOX_RING];   /* 32 */
    volatile uint8_t m2h[LINK_MBOX_RING];   /* 32 + LINK_MBOX_RING */
} link_mbox_t;

_Static_assert(offsetof(link_mbox_t, h2m_head) == 8, "mbox layout");
_Static_assert(offsetof(link_mbox_t, mcu_ctl) == 28, "mbox layout");
_Static_assert(offsetof(link_mbox_t, h2m) == 32, "mbox layout");
_Static_assert(offsetof(link_mbox_t, m2h) == 32 + LINK_MBOX_RING, "mbox layout");
_Static_assert(sizeof(link_mbox_t) == 32 + 2 * LINK_MBOX_RING, "mbox layout");

#if defined(__arm__)
#define LINK_MB() __asm volatile("dmb" ::: "memory")
#else
#define LINK_MB() __atomic_thread_fence(__ATOMIC_SEQ_CST)
#endif

/* MCU side. */
static inline void link_mbox_init(link_mbox_t *m)
{
    m->magic = 0;
    LINK_MB();
    m->h2m_tail = m->h2m_head < LINK_MBOX_RING ? m->h2m_head : 0;
    m->m2h_head = 0;
    m->mcu_ctl = 0;
    m->version = LINK_MBOX_VERSION;
    m->ring_size = LINK_MBOX_RING;
    LINK_MB();
    m->magic = LINK_MBOX_MAGIC;
}

/* Bytes received from the host; -1 if empty. */
static inline int link_mbox_getc(link_mbox_t *m)
{
    uint32_t tail = m->h2m_tail, head = m->h2m_head;
    if (head == tail || head >= LINK_MBOX_RING)
        return -1;
    LINK_MB();                                /* head before data */
    int c = m->h2m[tail];
    LINK_MB();                                /* data before tail */
    m->h2m_tail = tail + 1u == LINK_MBOX_RING ? 0u : tail + 1u;
    return c;
}

/* Queue one byte for the host; false if the ring is full. */
static inline bool link_mbox_putc(link_mbox_t *m, uint8_t c)
{
    uint32_t head = m->m2h_head, next = head + 1u == LINK_MBOX_RING ? 0u : head + 1u;
    if (next == m->m2h_tail)
        return false;
    m->m2h[head] = c;
    LINK_MB();                                /* data before head */
    m->m2h_head = next;
    return true;
}

/* "Power" the CBU (PB05 low) or cut it. Switching on discards host bytes left
 * from the last session, as a real power cut would. */
static inline void link_mbox_power(link_mbox_t *m, bool on)
{
    if (on) {
        uint32_t head = m->h2m_head;
        m->h2m_tail = head < LINK_MBOX_RING ? head : 0;
        LINK_MB();
        m->mcu_ctl = (m->mcu_ctl & ~LINK_MBOX_MCU_FAST) | LINK_MBOX_MCU_CBU_ON;
    } else {
        m->mcu_ctl &= ~(LINK_MBOX_MCU_CBU_ON | LINK_MBOX_MCU_FAST);
    }
}

static inline bool link_mbox_booted(const link_mbox_t *m)
{
    return (m->mcu_ctl & LINK_MBOX_MCU_CBU_ON) && (m->host_ctl & LINK_MBOX_HOST_BOOTED);
}

#ifdef LINK_MBOX
extern link_mbox_t link_mbox;
#endif
