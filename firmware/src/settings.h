/* User settings set by CONFIG (docs/protocol.md 4.6), stored in the two
 * settings sectors 0x1FC00 / 0x1FE00 as a ping-pong pair (design 4.4).
 *
 * Record at the start of a sector (all little-endian):
 *
 *   off size field
 *    0   4   magic    0x53354854 ("TH5S"; stock's sector has no such magic)
 *    4   2   version  1
 *    6   2   len      bytes of TLV that follow (v1: 27)
 *    8   4   seq      +1 per write; the valid record with the higher seq wins
 *   12  len  tlv      the canonical CONFIG TLV encoding of every tag, then
 *                     the MCU-local tags (0x81 UNITS u8: the C/F button
 *                     setting); len 30 (27 in records written before UNITS)
 *  12+len 4  crc32    zlib CRC-32 over bytes 0 .. 11+len
 *
 * Local tags sit in the reserved range 0x80-0xFF: never sent or accepted on
 * the wire and not part of settings_crc(), so the C/F button cannot make
 * HA's settings CRC differ.
 *
 * The rest of the sector stays erased (notes-flash.md: the last words of
 * flash are left 0xFF). Storing the canonical TLV makes settings_crc() the
 * CRC-32 of the stored bytes and lets a newer firmware add tags. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "link_proto.h"

#define SETTINGS_MAGIC   0x53354854u
#define SETTINGS_VERSION 1u
#define SETTINGS_TLV_LEN 27u          /* 8 tags: 5 x (2+1) + 3 x (2+2) */
#define SETTINGS_TAG_UNITS 0x81u      /* local: 0 degC, 1 degF */
#define SETTINGS_REC_TLV_LEN (SETTINGS_TLV_LEN + 3u)

typedef struct {
    int32_t v[LINK_CFG_TAG_COUNT + 1]; /* indexed by LINK_CFG_*; [0] unused */
    uint8_t units;                     /* local (C/F button), not in the CRC */
} settings_t;

/* Load the newest valid record, or the defaults if there is none (then
 * settings_factory() is true). */
void settings_init(void);
const settings_t *settings_get(void);
bool settings_factory(void);       /* no valid record was found at boot */

/* Defaults and per-tag limits (protocol.md 4.6). */
void settings_defaults(settings_t *s);
/* Payload length of a tag's value (1 or 2), 0 for an unknown tag. */
uint8_t settings_tag_len(uint8_t tag);
bool settings_tag_ok(uint8_t tag, int32_t value);

/* Canonical TLV (all 8 tags, ascending) into out[SETTINGS_TLV_LEN]. */
void settings_encode(const settings_t *s, uint8_t out[SETTINGS_TLV_LEN]);
uint32_t settings_crc(const settings_t *s);

/* Write s to the sector not holding the current record (erase, program,
 * read back, check) and make it current. Flash ops: needs flash_init().
 * Returns 0 on success, else the flash address that failed. */
uint32_t settings_save(const settings_t *s);

/* Store the display units (C/F button). No flash write if unchanged.
 * Returns 0 on success, else the failing flash address. */
uint32_t settings_set_units(uint8_t units);

/* Factory reset: erase both settings sectors and fall back to the defaults
 * (settings_factory() is then true). False if an erase failed. */
bool settings_erase(void);

/* Diagnostics for the bench: the sector of the current record (0: none)
 * and its seq. */
uint32_t settings_sector(void);
uint32_t settings_seq(void);
