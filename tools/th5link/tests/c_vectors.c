/* Host cross-check of firmware/src/link_proto.h against tools/th5link/proto.py.
 *   clang -std=c11 -Wall -Wextra -Werror -Ifirmware/src tools/th5link/tests/c_vectors.c -o c_vectors
 *   ./c_vectors emit          one "name hex" line per vector
 *   ./c_vectors check < hex   one line per input frame: payload length or -1, plus fields
 *   ./c_vectors mbox          ring self-test (wrap, full, empty)
 * Driven by tools/th5link/tests/test_c_crosscheck.py. */
#include <stdio.h>
#include <string.h>

#include "link_proto.h"

static uint8_t buf[LINK_RX_BUF];

static void hex(const char *name, const uint8_t *p, uint32_t n)
{
    printf("%s ", name);
    for (uint32_t i = 0; i < n; i++)
        printf("%02x", p[i]);
    printf("\n");
}

static void emit(void)
{
    uint8_t *p = buf + LINK_HDR_BYTES;
    printf("crc16 %04x\n", link_crc16(0xFFFFu, (const uint8_t *)"123456789", 9));

    p[LINK_HELLO_O_PROTO_MAX] = 1;
    p[LINK_HELLO_O_FLAGS] = 0;
    link_put16(p + LINK_HELLO_O_RX_MAX, LINK_MAX_TO_CBU);
    link_put32(p + LINK_HELLO_O_FW, 0x00010203u);
    hex("hello", buf, link_frame_seal(buf, 0, LINK_CMD_HELLO, LINK_HELLO_LEN));

    memset(p, 0, LINK_HELLO_RSP_LEN);
    p[LINK_HR_O_PROTO] = 1;
    p[LINK_HR_O_RESET_CAUSE] = LINK_RST_POR | LINK_RST_BOR;
    link_put32(p + LINK_HR_O_BAUD, LINK_BAUD_FAST);
    link_put16(p + LINK_HR_O_RX_MAX, LINK_MAX_TO_MCU);
    link_put32(p + LINK_HR_O_FW, 0x00010000u);
    link_put64(p + LINK_HR_O_FRAME_ID, 0x1122334455667788ull);
    link_put32(p + LINK_HR_O_SETTINGS_CRC, 0xDEADBEEFu);
    p[LINK_HR_O_UNITS] = 1;
    p[LINK_HR_O_TRIGGER] = LINK_TRIG_BUTTON;
    link_put16(p + LINK_HR_O_FLAGS, LINK_F_TIME_VALID | LINK_F_FRAME_VALID | LINK_F_SENSOR_OK);
    link_put16(p + LINK_HR_O_T, (uint16_t)(int16_t)-1234);
    link_put16(p + LINK_HR_O_RH, 5280);
    link_put16(p + LINK_HR_O_BATT_MV, 3980);
    p[LINK_HR_O_BATT_PCT] = 78;
    p[LINK_HR_O_CHARGE] = LINK_CHG_CHARGING;
    link_put32(p + LINK_HR_O_LAST_MS, 4321);
    p[LINK_HR_O_LAST_RESULT] = LINK_RES_OK;
    p[LINK_HR_O_FAIL_COUNT] = 0;
    p[LINK_HR_O_BOOT_VER] = 1;
    hex("hello_rsp", buf, link_frame_seal(buf, 0, LINK_CMD_HELLO_RSP, LINK_HELLO_RSP_LEN));

    p[0] = LINK_NET_API_UP;
    p[1] = 0;
    p[2] = (uint8_t)(int8_t)-58;
    hex("net", buf, link_frame_seal(buf, 3, LINK_CMD_NET, LINK_NET_LEN));

    link_put32(p + LINK_TIME_O_UTC, 1793520000u);
    link_put16(p + LINK_TIME_O_OFFSET, 60);
    link_put32(p + LINK_TIME_O_NEXT_UTC, 1806195600u);
    link_put16(p + LINK_TIME_O_NEXT_OFFSET, 120);
    hex("time", buf, link_frame_seal(buf, 4, LINK_CMD_TIME, LINK_TIME_LEN));

    uint8_t *q = p;
    *q++ = LINK_CFG_MODE; *q++ = 1; *q++ = 1;
    *q++ = LINK_CFG_SYNC_MIN; *q++ = 2; link_put16(q, 60); q += 2;
    *q++ = LINK_CFG_CLOCK_MIN; *q++ = 1; *q++ = 5;
    *q++ = LINK_CFG_CLOCK_24H; *q++ = 1; *q++ = 1;
    *q++ = LINK_CFG_FULL_N; *q++ = 1; *q++ = 30;
    *q++ = LINK_CFG_T_OFF; *q++ = 2; link_put16(q, (uint16_t)(int16_t)-50); q += 2;
    *q++ = LINK_CFG_RH_OFF; *q++ = 2; link_put16(q, 120); q += 2;
    *q++ = LINK_CFG_FLAGS; *q++ = 1; *q++ = LINK_CFGF_BADGE;
    hex("config", buf, link_frame_seal(buf, 5, LINK_CMD_CONFIG, (uint16_t)(q - p)));

    link_put64(p + LINK_IMGB_O_FRAME_ID, 0x0123456789ABCDEFull);
    p[LINK_IMGB_O_BPP] = 2;
    p[LINK_IMGB_O_ENCODING] = 0;
    p[LINK_IMGB_O_FLAGS] = 1;
    p[LINK_IMGB_O_RESERVED] = 0;
    link_put32(p + LINK_IMGB_O_LENGTH, 24960);
    link_put32(p + LINK_IMGB_O_CRC32, 0xCAFEBABEu);
    hex("img_begin", buf, link_frame_seal(buf, 7, LINK_CMD_IMG_BEGIN, LINK_IMG_BEGIN_LEN));

    link_put32(p + LINK_IMGD_O_OFFSET, LINK_IMG_CHUNK);
    for (unsigned i = 0; i < 8; i++)
        p[LINK_IMGD_O_DATA + i] = (uint8_t)(0xF0 + i);
    hex("img_data", buf, link_frame_seal(buf, 8, LINK_CMD_IMG_DATA, 12));

    link_put64(p, 0x0123456789ABCDEFull);
    hex("img_end", buf, link_frame_seal(buf, 9, LINK_CMD_IMG_END, LINK_IMG_END_LEN));

    link_put16(p, 900);
    hex("hold", buf, link_frame_seal(buf, 10, LINK_CMD_HOLD, LINK_HOLD_LEN));

    p[0] = LINK_BYE_DONE;
    hex("bye", buf, link_frame_seal(buf, 11, LINK_CMD_BYE, LINK_BYE_LEN));

    hex("forget", buf, link_frame_seal(buf, 0, LINK_CMD_FORGET, 0));

    link_put32(p + LINK_FWB_O_VERSION, 0x00020001u);
    link_put32(p + LINK_FWB_O_LENGTH, 32572);
    link_put32(p + LINK_FWB_O_CRC32, 0x0BADF00Du);
    link_put32(p + LINK_FWB_O_LOAD, 0x1000);
    hex("fw_begin", buf, link_frame_seal(buf, 12, LINK_CMD_FW_BEGIN, LINK_FW_BEGIN_LEN));

    link_put32(p, 0x0BADF00Du);
    hex("fw_end", buf, link_frame_seal(buf, 13, LINK_CMD_FW_END, LINK_FW_END_LEN));

    p[LINK_ACK_O_CMD] = LINK_CMD_IMG_DATA;
    link_put32(p + LINK_ACK_O_DATA, 1028);
    hex("ack_img_data", buf, link_frame_seal(buf, 8, LINK_CMD_ACK, 5));

    p[LINK_ERR_O_CMD] = LINK_CMD_IMG_END;
    p[LINK_ERR_O_CODE] = LINK_E_CRC;
    link_put32(p + LINK_ERR_O_DETAIL, 0x12345678u);
    hex("err", buf, link_frame_seal(buf, 9, LINK_CMD_ERR, LINK_ERR_LEN));
}

static int unhex(const char *s, uint8_t *out, uint32_t max)
{
    uint32_t n = 0;
    unsigned v;
    while (*s && *s != '\n' && n < max && sscanf(s, "%2x", &v) == 1) {
        out[n++] = (uint8_t)v;
        s += 2;
    }
    return (int)n;
}

static void check(void)
{
    static char line[2 * LINK_RX_BUF + 16];
    while (fgets(line, sizeof line, stdin)) {
        int n = unhex(line, buf, sizeof buf);
        int32_t len = link_frame_check(buf, (uint32_t)n, LINK_MAX_TO_MCU);
        printf("%d", (int)len);
        const uint8_t *p = buf + LINK_HDR_BYTES;
        if (len >= 0) {
            printf(" seq=%u cmd=%u", buf[2], buf[3]);
            if (buf[3] == LINK_CMD_HELLO_RSP && len >= (int32_t)LINK_HELLO_RSP_LEN)
                printf(" baud=%u frame_id=%016llx crc=%08x t=%d rh=%u mv=%u flags=%u",
                       (unsigned)link_get32(p + LINK_HR_O_BAUD),
                       (unsigned long long)link_get64(p + LINK_HR_O_FRAME_ID),
                       (unsigned)link_get32(p + LINK_HR_O_SETTINGS_CRC),
                       (int)(int16_t)link_get16(p + LINK_HR_O_T), link_get16(p + LINK_HR_O_RH),
                       link_get16(p + LINK_HR_O_BATT_MV), link_get16(p + LINK_HR_O_FLAGS));
            if (buf[3] == LINK_CMD_TIME && len >= (int32_t)LINK_TIME_LEN)
                printf(" utc=%u off=%d next=%u next_off=%d", (unsigned)link_get32(p + LINK_TIME_O_UTC),
                       (int)(int16_t)link_get16(p + LINK_TIME_O_OFFSET),
                       (unsigned)link_get32(p + LINK_TIME_O_NEXT_UTC),
                       (int)(int16_t)link_get16(p + LINK_TIME_O_NEXT_OFFSET));
            if (buf[3] == LINK_CMD_IMG_BEGIN && len >= (int32_t)LINK_IMG_BEGIN_LEN)
                printf(" id=%016llx bpp=%u len=%u crc=%08x",
                       (unsigned long long)link_get64(p + LINK_IMGB_O_FRAME_ID), p[LINK_IMGB_O_BPP],
                       (unsigned)link_get32(p + LINK_IMGB_O_LENGTH),
                       (unsigned)link_get32(p + LINK_IMGB_O_CRC32));
            if (buf[3] == LINK_CMD_HELLO_RSP && len >= (int32_t)LINK_HELLO_RSP_LEN)
                printf(" boot_ver=%u", p[LINK_HR_O_BOOT_VER]);
            if (buf[3] == LINK_CMD_FW_BEGIN && len >= (int32_t)LINK_FW_BEGIN_LEN)
                printf(" ver=%08x len=%u crc=%08x load=%08x", (unsigned)link_get32(p + LINK_FWB_O_VERSION),
                       (unsigned)link_get32(p + LINK_FWB_O_LENGTH),
                       (unsigned)link_get32(p + LINK_FWB_O_CRC32),
                       (unsigned)link_get32(p + LINK_FWB_O_LOAD));
        }
        printf("\n");
    }
}

static int mbox(void)
{
    static link_mbox_t m;
    memset((void *)&m, 0, sizeof m);
    link_mbox_init(&m);
    if (m.magic != LINK_MBOX_MAGIC || m.ring_size != LINK_MBOX_RING)
        return 1;
    link_mbox_power(&m, true);
    m.host_ctl = LINK_MBOX_HOST_BOOTED;
    if (!link_mbox_booted(&m))
        return 2;
    /* host -> MCU: push 5000 bytes through in bursts of up to ring - 1 */
    uint32_t sent = 0, got = 0;
    while (got < 5000) {
        uint32_t head = m.h2m_head, tail = m.h2m_tail;
        uint32_t free = (tail + LINK_MBOX_RING - head - 1) % LINK_MBOX_RING;
        for (uint32_t i = 0; i < free && sent < 5000; i++, sent++) {
            m.h2m[head] = (uint8_t)(sent * 7);
            head = (head + 1) % LINK_MBOX_RING;
        }
        m.h2m_head = head;
        int c;
        while ((c = link_mbox_getc(&m)) >= 0) {
            if (c != (uint8_t)(got * 7))
                return 3;
            got++;
        }
    }
    /* MCU -> host: fill until full, check capacity, drain */
    uint32_t n = 0;
    while (link_mbox_putc(&m, (uint8_t)n))
        n++;
    if (n != LINK_MBOX_RING - 1)
        return 4;
    for (uint32_t i = 0; i < n; i++) {
        if (m.m2h[m.m2h_tail] != (uint8_t)i)
            return 5;
        m.m2h_tail = (m.m2h_tail + 1) % LINK_MBOX_RING;
    }
    if (!link_mbox_putc(&m, 0xAA))
        return 6;
    link_mbox_power(&m, false);
    if (link_mbox_booted(&m))
        return 7;
    printf("mbox ok\n");
    return 0;
}

int main(int argc, char **argv)
{
    if (argc > 1 && !strcmp(argv[1], "emit"))
        emit();
    else if (argc > 1 && !strcmp(argv[1], "check"))
        check();
    else if (argc > 1 && !strcmp(argv[1], "mbox"))
        return mbox();
    else
        return 2;
    return 0;
}
