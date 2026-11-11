/* MCU firmware update: memory map, info blocks and the staging header,
 * shared by the bootloader (boot/boot.c) and the app (link.c). Design 4.7,
 * protocol 4.14-4.15.
 *
 *   0x00000..0x00FFF  bootloader; info block at 0x000C0 (TH5B, boot_ver)
 *   0x01000..0x0FFFF  app; vectors at 0x01000, info block at 0x010C0 (TH5A)
 *   0x10000..0x1C3FF  staging = frame slots A and B: 32 B header, then image
 *
 * Staging header, little-endian:
 *    0 magic 'TH5U'   4 fw_version   8 length   12 crc32 (zlib, image)
 *   16 load 0x1000   20 min_boot_ver
 *   24 commit  0xFFFFFFFF until the app checked the CRC over flash, then 0
 *   28 done    0xFFFFFFFF until the bootloader installed the image, then 0 */
#pragma once
#include <stdint.h>

#define FWU_BOOT_BASE    0x00000000u
#define FWU_APP_BASE     0x00001000u
#define FWU_APP_END      0x00010000u
#define FWU_STAGE        0x00010000u
#define FWU_STAGE_END    0x0001C400u
#define FWU_HDR_BYTES    32u
#define FWU_MAX_IMAGE    (FWU_STAGE_END - FWU_STAGE - FWU_HDR_BYTES) /* 50,144 */
#define FWU_MIN_IMAGE    64u

#define FWU_INFO_OFF     0xC0u        /* after the 48 vectors */
#define FWU_MAGIC        0x55354854u  /* "TH5U" */
#define FWU_BOOT_MAGIC   0x42354854u  /* "TH5B" */
#define FWU_APP_MAGIC    0x41354854u  /* "TH5A" */
#define FWU_BOOT_VER     1u
#define FWU_ERASED       0xFFFFFFFFu
#define FWU_SET          0x00000000u

#define FWU_RAM_BASE     0x20000000u
#define FWU_RAM_END      0x20004000u

typedef struct {
    uint32_t magic;
    uint32_t fw_version;
    uint32_t length;
    uint32_t crc32;
    uint32_t load;
    uint32_t min_boot_ver;
    uint32_t commit;
    uint32_t done;
} fwu_hdr_t;

/* At FWU_INFO_OFF in both the bootloader and the app. */
typedef struct {
    uint32_t magic;   /* FWU_BOOT_MAGIC or FWU_APP_MAGIC */
    uint32_t version; /* boot_ver, or the app's fw_version */
} fwu_info_t;

_Static_assert(sizeof(fwu_hdr_t) == FWU_HDR_BYTES, "staging header");
_Static_assert(FWU_MAX_IMAGE == 50144u, "staging = slots A + B minus the header");
