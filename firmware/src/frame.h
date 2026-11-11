/* Stored frame format, shared with the Home Assistant renderer
 * (custom_components/th5_display/frame.py). Design 4.4 and 5.2.
 *
 * A slot is a 32-byte header followed by the payload:
 *
 *   off size field
 *    0   4   magic      0x46354854 ("TH5F" little-endian)
 *    4   1   version    1
 *    5   1   bpp        1 (plane0 only) or 2 (plane0 then plane1)
 *    6   1   encoding   0 = raw (PackBits reserved, not implemented)
 *    7   1   flags      bit 0: forecast layout, the MCU draws its live band
 *                       over landscape y 0..55 (bytes 0..6 of every row)
 *    8   4   seq        higher wins when both slots are valid
 *   12   8   frame_id   opaque to the MCU, chosen by HA (content hash)
 *   20   4   length     payload bytes: 12480 * bpp
 *   24   4   crc32      zlib CRC-32 over the payload
 *   28   4   commit     0xFFFFFFFF while writing, 0 once the CRC passed
 *
 * All fields little-endian. Each plane is 416 rows of 30 bytes in panel
 * order: row = landscape x (0 = left), byte k bit 7-b = landscape y 8k+b
 * (0 = top), as render.h. Pixel level = 2*p0 + p1: 3 white, 2 light grey,
 * 1 dark grey, 0 black. plane0 -> DTM1 (0x10), plane1 -> DTM2 (0x13). A
 * 1 bpp frame is plane0 only and shows as plain black/white. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define FRAME_MAGIC       0x46354854u
#define FRAME_VERSION     1u
#define FRAME_PLANE_BYTES (416u * 30u)
#define FRAME_HDR_BYTES   32u
#define FRAME_FLAG_BAND   0x01u
#define FRAME_COMMITTED   0x00000000u
#define FRAME_BAND_BYTES  7u          /* landscape y 0..55 */

#define FRAME_SLOT_A      0x00010000u
#define FRAME_SLOT_B      0x00016200u
#define FRAME_SLOT_BYTES  0x6200u     /* 25,088 */

typedef struct {
    uint32_t magic;
    uint8_t version, bpp, encoding, flags;
    uint32_t seq;
    uint32_t id_lo, id_hi;
    uint32_t length;
    uint32_t crc32;
    uint32_t commit;
} frame_hdr_t;

/* Read access to flash by address (the host tests map it into an array). */
#ifdef LINK_HOST
extern uint8_t host_flash[];
#define FLASH_MEM(a) ((const void *)(host_flash + (a)))
#else
#define FLASH_MEM(a) ((const void *)(uintptr_t)(a))
#endif

/* The newest committed slot whose header and CRC check out, or NULL.
 * Cached; frame_invalidate() forces a fresh check (after a commit). */
const frame_hdr_t *frame_active(void);
void frame_invalidate(void);
uint64_t frame_id(const frame_hdr_t *h); /* 0 for NULL */
const uint8_t *frame_plane(const frame_hdr_t *h, unsigned plane);
uint32_t frame_crc32(uint32_t crc, const uint8_t *p, uint32_t n);
