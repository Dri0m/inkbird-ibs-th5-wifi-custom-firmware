/* Host model of the HC32L17x FLASH controller for test_flash.c.
 * Included by src/flash.c only when built with -DFLASH_HOST_TEST; supplies
 * the io_* accessors, irq_save/irq_restore and wdt_feed that the target
 * build gets from inline asm / wdt.h. Every MMIO write is logged.
 *
 * Model (from the same sources as notes-flash.md):
 * - BYPASS: 0x5A5A then 0xA5A5 arms one write to another FLASH register;
 *   an unarmed write to a protected register is dropped (and counted).
 * - CR: OP[1:0], BUSY reads 0 (operations complete instantly).
 * - SLOCKn bit i covers 2 KB group 32n + i (4 sectors); 1 = unlocked.
 * - A store into flash with OP = 2 erases the 512 B sector if its group is
 *   unlocked; with OP = 1 it ANDs the value in; otherwise it is a fault. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define HOST_FLASH_BYTES 0x20000u

typedef struct {
    uint32_t addr, val;
    uint8_t width;   /* 1 or 4 */
    bool masked;     /* IRQs masked at the time */
} host_wr_t;

extern uint8_t host_flash[HOST_FLASH_BYTES];
extern uint32_t host_regs[16];        /* 0x40020000 + 4*i */
extern uint32_t host_clken0;
extern host_wr_t host_log[4096];
extern unsigned host_nlog;
extern unsigned host_faults;          /* dropped / illegal operations */
extern unsigned host_feeds;
extern bool host_masked;

uint32_t io_rd32(uintptr_t a);
void io_wr32(uintptr_t a, uint32_t v);
uint8_t io_rd8(uintptr_t a);
void io_wr8(uintptr_t a, uint8_t v);
uint32_t irq_save(void);
void irq_restore(uint32_t pm);
void wdt_feed(void);
