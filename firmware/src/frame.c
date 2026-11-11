#include "frame.h"

/* zlib CRC-32 (reflected 0xEDB88320), a nibble at a time: 64 B of table,
 * about 0.15 s for a 2 bpp frame at 4 MHz. */
uint32_t frame_crc32(uint32_t crc, const uint8_t *p, uint32_t n)
{
    static const uint32_t t[16] = {
        0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC, 0x76DC4190, 0x6B6B51F4, 0x4DB26158, 0x5005713C,
        0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C, 0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C,
    };
    crc = ~crc;
    while (n--) {
        crc ^= *p++;
        crc = (crc >> 4) ^ t[crc & 15u];
        crc = (crc >> 4) ^ t[crc & 15u];
    }
    return ~crc;
}

static bool slot_ok(const frame_hdr_t *h)
{
    if (h->magic != FRAME_MAGIC || h->version != FRAME_VERSION || h->encoding != 0u ||
        (h->bpp != 1u && h->bpp != 2u) || h->commit != FRAME_COMMITTED ||
        h->length != h->bpp * FRAME_PLANE_BYTES)
        return false;
    return frame_crc32(0, (const uint8_t *)h + FRAME_HDR_BYTES, h->length) == h->crc32;
}

/* Flash only changes through a link session (which calls frame_invalidate
 * after a commit) or SWD followed by a reset, so the slots are checked once
 * and the answer kept. */
static const frame_hdr_t *active;
static bool checked;

void frame_invalidate(void) { checked = false; }

uint64_t frame_id(const frame_hdr_t *h)
{
    return h ? (uint64_t)h->id_hi << 32 | h->id_lo : 0u;
}

const frame_hdr_t *frame_active(void)
{
    if (!checked) {
        const frame_hdr_t *a = (const frame_hdr_t *)FLASH_MEM(FRAME_SLOT_A);
        const frame_hdr_t *b = (const frame_hdr_t *)FLASH_MEM(FRAME_SLOT_B);
        bool oa = slot_ok(a), ob = slot_ok(b);
        if (oa && ob) active = (int32_t)(b->seq - a->seq) > 0 ? b : a;
        else active = oa ? a : ob ? b : 0;
        checked = true;
    }
    return active;
}

const uint8_t *frame_plane(const frame_hdr_t *h, unsigned plane)
{
    if (plane >= h->bpp) plane = 0;
    return (const uint8_t *)h + FRAME_HDR_BYTES + plane * FRAME_PLANE_BYTES;
}
