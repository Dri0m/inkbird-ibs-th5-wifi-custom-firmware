/* Settings record in the ping-pong sectors (layout: settings.h). */
#include "settings.h"

#include <string.h>

#include "flash.h"
#include "frame.h"

#define REC_HDR   12u
#define REC_BYTES (REC_HDR + SETTINGS_REC_TLV_LEN + 4u)

static const struct { uint8_t len; int32_t lo, hi, def; } k_tags[LINK_CFG_TAG_COUNT + 1] = {
    [LINK_CFG_MODE]      = { 1, 0, 1, 1 },
    [LINK_CFG_SYNC_MIN]  = { 2, 15, 240, 60 },
    [LINK_CFG_CLOCK_MIN] = { 1, 1, 30, 1 },
    [LINK_CFG_CLOCK_24H] = { 1, 0, 1, 1 },
    [LINK_CFG_FULL_N]    = { 1, 1, 255, 30 },
    [LINK_CFG_T_OFF]     = { 2, -1000, 1000, 0 },
    [LINK_CFG_RH_OFF]    = { 2, -2000, 2000, 0 },
    [LINK_CFG_FLAGS]     = { 1, 0, 15, 0 },
};

static settings_t s_cur;
static uint32_t s_sector; /* address of the current record, 0 = none */
static uint32_t s_seq;
static bool s_factory;

uint8_t settings_tag_len(uint8_t tag)
{
    return tag >= 1u && tag <= LINK_CFG_TAG_COUNT ? k_tags[tag].len : 0u;
}

bool settings_tag_ok(uint8_t tag, int32_t value)
{
    return settings_tag_len(tag) && value >= k_tags[tag].lo && value <= k_tags[tag].hi;
}

void settings_defaults(settings_t *s)
{
    memset(s, 0, sizeof *s);
    for (uint8_t t = 1; t <= LINK_CFG_TAG_COUNT; t++)
        s->v[t] = k_tags[t].def;
}

void settings_encode(const settings_t *s, uint8_t out[SETTINGS_TLV_LEN])
{
    uint8_t *p = out;
    for (uint8_t t = 1; t <= LINK_CFG_TAG_COUNT; t++) {
        *p++ = t;
        *p++ = k_tags[t].len;
        if (k_tags[t].len == 1u) {
            *p++ = (uint8_t)s->v[t];
        } else {
            link_put16(p, (uint16_t)s->v[t]);
            p += 2;
        }
    }
}

uint32_t settings_crc(const settings_t *s)
{
    uint8_t tlv[SETTINGS_TLV_LEN];
    settings_encode(s, tlv);
    return frame_crc32(0, tlv, sizeof tlv);
}

/* A record at addr: valid header, CRC; TLV applied over the defaults (tags
 * with a bad length or value keep the default). */
static bool record_load(uint32_t addr, settings_t *out, uint32_t *seq)
{
    const uint8_t *r = FLASH_MEM(addr);
    uint32_t len = link_get16(r + 6);
    if (link_get32(r) != SETTINGS_MAGIC || link_get16(r + 4) != SETTINGS_VERSION ||
        len > FLASH_SECTOR_BYTES - REC_HDR - 4u)
        return false;
    if (frame_crc32(0, r, REC_HDR + len) != link_get32(r + REC_HDR + len))
        return false;
    settings_defaults(out);
    const uint8_t *t = r + REC_HDR;
    for (uint32_t i = 0; i + 2u <= len && i + 2u + t[i + 1] <= len; i += 2u + t[i + 1]) {
        uint8_t tag = t[i], n = t[i + 1];
        if (tag == SETTINGS_TAG_UNITS && n == 1u) {
            out->units = t[i + 2] ? 1u : 0u;
            continue;
        }
        if (settings_tag_len(tag) != n)
            continue;
        int32_t v = n == 1u ? (int32_t)t[i + 2]
                  : (tag == LINK_CFG_T_OFF || tag == LINK_CFG_RH_OFF) ? (int32_t)(int16_t)link_get16(t + i + 2)
                  : (int32_t)link_get16(t + i + 2);
        if (settings_tag_ok(tag, v))
            out->v[tag] = v;
    }
    *seq = link_get32(r + 8);
    return true;
}

void settings_init(void)
{
    settings_t a, b;
    uint32_t sa = 0, sb = 0;
    bool oa = record_load(FLASH_SETTINGS_A, &a, &sa);
    bool ob = record_load(FLASH_SETTINGS_B, &b, &sb);
    if (oa && ob) {
        bool use_b = (int32_t)(sb - sa) > 0;
        s_cur = use_b ? b : a;
        s_sector = use_b ? FLASH_SETTINGS_B : FLASH_SETTINGS_A;
        s_seq = use_b ? sb : sa;
    } else if (oa || ob) {
        s_cur = oa ? a : b;
        s_sector = oa ? FLASH_SETTINGS_A : FLASH_SETTINGS_B;
        s_seq = oa ? sa : sb;
    } else {
        settings_defaults(&s_cur);
        s_sector = 0;
        s_seq = 0;
    }
    s_factory = !oa && !ob;
}

const settings_t *settings_get(void) { return &s_cur; }
bool settings_factory(void) { return s_factory; }
uint32_t settings_sector(void) { return s_sector; }
uint32_t settings_seq(void) { return s_seq; }

static bool blank(uint32_t addr, uint32_t n)
{
    const uint32_t *p = FLASH_MEM(addr);
    for (uint32_t i = 0; i < n / 4u; i++)
        if (p[i] != 0xFFFFFFFFu)
            return false;
    return true;
}

uint32_t settings_save(const settings_t *s)
{
    uint32_t dst = s_sector == FLASH_SETTINGS_A ? FLASH_SETTINGS_B : FLASH_SETTINGS_A;
    uint32_t rec_words[(REC_BYTES + 3u) / 4u];
    uint8_t *rec = (uint8_t *)rec_words;
    memset(rec_words, 0xFF, sizeof rec_words);
    link_put32(rec, SETTINGS_MAGIC);
    link_put16(rec + 4, SETTINGS_VERSION);
    link_put16(rec + 6, SETTINGS_REC_TLV_LEN);
    link_put32(rec + 8, s_seq + 1u);
    settings_encode(s, rec + REC_HDR);
    uint8_t *loc = rec + REC_HDR + SETTINGS_TLV_LEN;
    loc[0] = SETTINGS_TAG_UNITS;
    loc[1] = 1u;
    loc[2] = s->units ? 1u : 0u;
    link_put32(rec + REC_HDR + SETTINGS_REC_TLV_LEN,
               frame_crc32(0, rec, REC_HDR + SETTINGS_REC_TLV_LEN));

    if (!blank(dst, FLASH_SECTOR_BYTES) && !flash_erase_sector(dst))
        return dst;
    if (!flash_program(dst, rec, REC_BYTES))
        return dst;
    settings_t back;
    uint32_t seq;
    if (!record_load(dst, &back, &seq))
        return dst;
    for (uint8_t t = 1; t <= LINK_CFG_TAG_COUNT; t++)
        if (back.v[t] != s->v[t])
            return dst;
    if (back.units != (s->units ? 1u : 0u))
        return dst;
    s_cur = back;
    s_sector = dst;
    s_seq = seq;
    s_factory = false;
    return 0;
}

uint32_t settings_set_units(uint8_t units)
{
    units = units ? 1u : 0u;
    if (s_cur.units == units)
        return 0;
    settings_t ns = s_cur;
    ns.units = units;
    return settings_save(&ns);
}

bool settings_erase(void)
{
    bool ok = true;
    if (!blank(FLASH_SETTINGS_A, FLASH_SECTOR_BYTES))
        ok = flash_erase_sector(FLASH_SETTINGS_A) && ok;
    if (!blank(FLASH_SETTINGS_B, FLASH_SECTOR_BYTES))
        ok = flash_erase_sector(FLASH_SETTINGS_B) && ok;
    settings_defaults(&s_cur);
    s_sector = 0;
    s_seq = 0;
    s_factory = true;
    return ok;
}
