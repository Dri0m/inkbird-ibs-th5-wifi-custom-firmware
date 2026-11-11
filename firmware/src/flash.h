/* In-application flash erase/program for the frame slots and the settings
 * sectors only (design/design.md 4.4). Register facts, sources and the
 * on-device test plan: firmware/notes-flash.md.
 *
 * Writable ranges (everything else is rejected before any register access):
 *   0x10000..0x1C3FF  frame slots A and B (49 sectors each), one range:
 *                     also the firmware staging area (fwu.h)
 *   0x1FC00..0x1FFFF  settings, two sectors (ping-pong)
 *
 * Call from the main loop only (it feeds the WDT), after board_init() and
 * wdt_start(), with HCLK = 4 or 24 MHz RCH. flash_init() writes the timings
 * for the HCLK in effect; after every clock change call it again (until
 * then erase/program are refused). */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define FLASH_SECTOR_BYTES   512u
#define FLASH_SETTINGS_A     0x0001FC00u
#define FLASH_SETTINGS_B     0x0001FE00u

/* Bus clock gate on, program/erase timings for the current SystemCoreClock
 * (4 or 24 MHz; anything else is refused), all sectors locked, controller
 * in read mode. False if a register does not read back. */
bool flash_init(void);

/* CR.WAIT: one read wait state (true) or none. Set it to true before HCLK
 * goes to 24 MHz and back to false after it returns to 4 MHz (clock_set_fast
 * does both). Kept by flash_init and every op. False if CR does not read
 * back. */
bool flash_set_wait(bool one);

/* True if [addr, addr+len) lies entirely inside one writable range.
 * len == 0 is rejected. Pure function, touches no register. */
bool flash_allowed(uint32_t addr, uint32_t len);

/* Erase the 512 B sector starting at addr (must be sector aligned and
 * allowed), then check it reads back all 0xFF. */
bool flash_erase_sector(uint32_t addr);

/* Program len bytes from src (any alignment) at addr. Words where addr is
 * 4-aligned, bytes for an unaligned head/tail. Bits can only go 1 -> 0:
 * a target that would need a 0 -> 1 change fails without programming
 * anything in that word/byte. Words/bytes already equal are skipped.
 * Every programmed unit is read back and compared. */
bool flash_program(uint32_t addr, const void *src, uint32_t len);
